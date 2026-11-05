"""POST /v1/jobs/v2: submit a Job Spec v2 (Kit) job (U7, docs/architecture/U7-scheduler-v2-design.md).

Validates with the U1 spec validator (which re-verifies the Kit signature on every use), stores the spec on
the job, then runs the same create / validate / hold / schedule flow as v1 ``POST /v1/jobs``. Nothing in the
v1 route, ledger, pricing or billing is changed: holds and spend limits are the existing functions.
"""

from __future__ import annotations

import decimal
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.jobs import _assert_project_accessible, shape_job
from app.core.config import settings
from app.db.session import get_db
from app.deps import client_ip, require_verified_user
from app.models.data_plane import DataFile
from app.models.jobs import Job
from app.models.provider import ProviderMachine
from app.schemas.auth import CurrentUser
from app.schemas.jobs import JobOut
from app.services import job_fsm as fsm
from app.services import scheduler_v2
from app.services.audit import record_audit
from app.services.kits import KitError, validate_job_spec_v2
from app.services.ledger import InsufficientFunds, lock_customer, to_minor
from app.services.scheduler import JobValidationError, schedule_job, validate_job
from app.services.spend_limits import SpendLimitExceeded, spend_guard

router = APIRouter(prefix="/v1/jobs", tags=["jobs-v2"])


async def build_v2_plan(db: AsyncSession, owner_id: uuid.UUID, spec: Any, kit: Any) -> dict:
    """The job's v2 plan, stored at ``job.data_policy["v2_plan"]`` and read by agent_v2.v2_plan_of (U3 contract).

    An input source ``dataset:<uuid>`` names one of the customer's data-plane files (U2). The file must be
    theirs, not deleted, and not expired; its size and SHA-256 come from the database, never from the client.
    Egress, assets and checkpoint come from the signed Kit manifest (absent = no network, no assets, no checkpoint). Raises KitError(422) with a per-field error for an unusable input.
    """
    import datetime as _dt

    errors: list[dict] = []
    inputs: list[dict] = []
    now = _dt.datetime.now(_dt.timezone.utc)
    for idx, ref in enumerate(spec.inputs):
        kind, _, ident = ref.source.partition(":")
        file_row = None
        if kind == "dataset":
            try:
                fid = uuid.UUID(ident)
            except ValueError:
                fid = None
            if fid is not None:
                file_row = (
                    await db.execute(select(DataFile).where(DataFile.id == fid, DataFile.owner_id == owner_id))
                ).scalar_one_or_none()
        if file_row is None or file_row.deleted_at is not None or (
            file_row.expires_at is not None and (
                file_row.expires_at if file_row.expires_at.tzinfo else file_row.expires_at.replace(tzinfo=_dt.timezone.utc)
            ) <= now
        ):
            errors.append({"field": f"inputs[{idx}].source", "code": "input_not_found",
                           "message": "source must be dataset:<id of one of your uploaded files>"})
            continue
        inputs.append({
            "name": ref.name, "file_id": str(file_row.id), "sha256": file_row.sha256,
            "size_bytes": int(file_row.size_bytes), "cacheable": False,
        })
    if errors:
        raise KitError("invalid_spec", 422, errors)

    wanted = {o.name for o in spec.outputs}
    outputs = [
        # A Kit manifest names outputs as "/out/<path>" (validated at publish). The agent plan wants the path RELATIVE
        # to its output dir (U3 design) and refuses a leading "/", so every Kit job failed invalid_v2_plan before this.
        {"name": o["name"], "path": o["path"].removeprefix("/out/").lstrip("/"),
         "required": (not wanted or o["name"] in wanted)}
        for o in kit.manifest["outputs"]
        if not wanted or o["name"] in wanted
    ]
    man = kit.manifest or {}
    assets: list[dict] = []
    for idx, a in enumerate(man.get("assets") or []):
        if a.get("file_id") is None:
            # A bare https url is accepted in the manifest, but the agent only fetches data-plane files (U3).
            errors.append({"field": f"manifest.assets[{idx}]", "code": "asset_url_not_supported_yet",
                           "message": "this Kit's asset is a bare url; the agent fetches data-plane files only"})
            continue
        row = (await db.execute(select(DataFile).where(
            DataFile.id == uuid.UUID(str(a["file_id"])), DataFile.owner_id == uuid.UUID(str(a["owner_id"]))
        ))).scalar_one_or_none()
        if row is None or row.deleted_at is not None or row.sha256 != a["sha256"] or int(row.size_bytes) != int(a["size_bytes"]):
            errors.append({"field": f"manifest.assets[{idx}]", "code": "asset_unavailable",
                           "message": "the Kit's asset is missing or does not match its pinned hash"})
            continue
        assets.append({"name": a["name"], "file_id": str(row.id), "owner_id": str(row.owner_id),
                       "sha256": row.sha256, "size_bytes": int(row.size_bytes), "cacheable": a.get("cacheable", True)})
    if errors:
        raise KitError("invalid_spec", 422, errors)
    ck = man.get("checkpoint")
    return {
        "kit": {"slug": kit.slug, "version": kit.version},
        "inputs": inputs,
        "assets": assets,
        "outputs": outputs,
        "egress": list(man.get("egress") or []),
        "checkpoint": {"dir": ck["dir"], "interval_seconds": int(ck["interval_seconds"])} if ck else None,
    }


class JobV2Create(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: dict[str, Any]
    project_id: uuid.UUID | None = None
    machine_id: uuid.UUID | None = None
    priority: int = Field(default=0, ge=0, le=1_000_000)
    # U15: optional unit quote to link to this job (only honoured when PRICING_UNITS_ENABLED).
    quote_id: uuid.UUID | None = None
    # Privacy tiers are refused for Kit jobs in this slice (they need encrypted_input / dk_shares); see design note.
    isolation_tier: str = Field(default="container", pattern="^(container|open|gvisor|microvm|sealed|sealed_pro|confidential)$")
    required_gpu_runtime: str = Field(default="cuda", pattern="^(cuda|rocm|any)$")


class JobV2Out(JobOut):
    kit: str | None = None
    spec_v2: dict | None = None


def _shape_v2(job: Job) -> JobV2Out:
    base = shape_job(job).model_dump()
    return JobV2Out(**base, kit=scheduler_v2.job_kit_ref(job), spec_v2=job.spec_v2)


@router.post("/v2", response_model=JobV2Out, status_code=status.HTTP_201_CREATED)
async def create_job_v2(
    body: JobV2Create,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> JobV2Out:
    await lock_customer(db, current.id)

    if idempotency_key:
        prior = (
            await db.execute(select(Job).where(Job.customer_id == current.id, Job.idempotency_key == idempotency_key))
        ).scalar_one_or_none()
        if prior is not None:
            return _shape_v2(prior)

    # Fail closed: privacy tiers are not offered for Kit jobs yet.
    if body.isolation_tier not in ("container", "open"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="privacy_tier_not_supported_for_kits")

    from app.services.abuse import assert_can_submit  # U16: no-op unless ABUSE_ENFORCE=true

    await assert_can_submit(db, current.id)
    await _assert_project_accessible(db, body.project_id, current)

    if body.machine_id is not None:
        known = await db.scalar(
            select(ProviderMachine.id).where(ProviderMachine.id == body.machine_id, ProviderMachine.status != "deregistered")
        )
        if known is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="machine_not_found")

    try:
        validated = await validate_job_spec_v2(db, body.spec)
    except KitError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"errors": exc.errors})

    spec, kit, eff = validated.spec, validated.kit, validated.effective_resources
    if int(eff.get("node_count") or 1) > 1 and body.isolation_tier not in ("container", "open"):
        # U13: multi-node never runs on sealed / sealed_pro / confidential (they refuse network; U18 does not cover it).
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"errors": [{
            "field": "isolation_tier", "code": "multi_node_private_tier_refused",
            "message": "multi-node jobs cannot use sealed, sealed_pro or confidential tiers"}]})
    try:
        v2_plan = await build_v2_plan(db, current.id, spec, kit)
    except KitError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"errors": exc.errors})
    try:
        gpu_count = scheduler_v2.stored_gpu_count(eff)  # gpu_count 0 (CPU-only Kit): refused unless CPU_ONLY_KITS_ENABLED
    except scheduler_v2.CpuOnlyKitsDisabled:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"errors": [{
            "field": "resources.gpu.count", "code": "cpu_only_kits_disabled",
            "message": "CPU-only Kits are not enabled on this platform"}]})
    node_count = int(eff.get("node_count") or 1)
    expected_units = spec.billing.expected_units
    per_rank_minutes = scheduler_v2.estimate_minutes(kit.manifest, expected_units, gpu_count)
    max_cost: decimal.Decimal | None = spec.billing.max_cost
    run_max_cost = max_cost
    if node_count > 1:
        # U13: billing.max_cost caps the WHOLE run; every rank job gets an equal share (rounded down to cents).
        max_cost = scheduler_v2.per_rank_max_cost(max_cost, node_count)
    job = Job(
        customer_id=current.id,
        project_id=body.project_id,
        workload_type="kit",
        model=f"{kit.slug}@{kit.version}",
        input={},
        status=fsm.CREATED,
        gpu_count=gpu_count,
        estimated_minutes=per_rank_minutes,  # the per-rank hold; the multi-node total is exposed in spec_v2.hold_estimate
        max_cost=max_cost,
        max_runtime_seconds=settings.JOB_MAX_RUNTIME_DEFAULT,
        retry_policy={"max_retries": 0},
        priority=body.priority,
        execution_mode="hardware",
        isolation_tier=body.isolation_tier,
        required_gpu_runtime=body.required_gpu_runtime.lower(),
        network_policy=None,
        data_policy={"v2_plan": v2_plan},
        idempotency_key=idempotency_key,
        machine_id=body.machine_id,
        spec_v2={
            "spec": spec.model_dump(mode="json"),
            "effective_resources": eff,
            "kit_manifest_sha256": kit.manifest_sha256,
            "kit_image": kit.image_digest,
            # U13: every rank is its own job with its own hold, so a multi-node run costs node_count x the per-job hold.
            **({"hold_estimate": {"per_rank_minutes": per_rank_minutes, "node_count": node_count,
                                  "total_minutes": per_rank_minutes * node_count,
                                  "run_max_cost": str(run_max_cost) if run_max_cost is not None else None,
                                  "rank_max_cost": str(max_cost)}} if node_count > 1 else {}),
        },
        kit_id=kit.slug,
        kit_version=kit.version,
    )
    db.add(job)
    await db.flush()

    if body.quote_id is not None and settings.PRICING_UNITS_ENABLED:
        from app.services import unit_quotes

        try:
            await unit_quotes.link_quote_to_job(db, quote_id=body.quote_id, customer_id=current.id, job=job)
        except unit_quotes.UnitQuoteError as exc:
            await db.rollback()
            raise HTTPException(status_code=exc.http_status, detail=exc.code)

    await fsm.transition(db, job, fsm.VALIDATING, reason="submitted", actor="customer")
    try:
        await validate_job(db, job)
    except JobValidationError as exc:
        await fsm.transition(db, job, fsm.FAILED, reason=exc.code, actor="system")
        job.error_code = exc.code
        job.error_message = f"validation failed: {exc.code}"
        await record_audit(
            db, action="job.fail", outcome="failure", actor_user_id=current.id,
            ip=client_ip(request), resource_type="job", resource_id=str(job.id), reason=exc.code,
        )
        await db.commit()
        raise HTTPException(status_code=exc.http_status, detail=exc.code)

    await fsm.transition(db, job, fsm.QUEUED, reason="validated", actor="system")
    await record_audit(
        db, action="job.create", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="job", resource_id=str(job.id),
    )

    authorized_minor = to_minor(job.max_cost) if job.max_cost is not None else 0
    try:
        async with spend_guard(db, current.id, job.project_id, authorized_minor):
            await schedule_job(db, job)
            await db.commit()
    except SpendLimitExceeded as exc:
        await fsm.transition(db, job, fsm.FAILED, reason="spend_limit_exceeded", actor="system")
        job.error_code = "spend_limit_exceeded"
        job.error_message = "job would exceed a configured spend limit"
        await record_audit(
            db, action="job.fail", outcome="failure", actor_user_id=current.id,
            ip=client_ip(request), resource_type="job", resource_id=str(job.id),
            reason=f"spend_limit:{exc.scope}/{exc.period}",
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="spend_limit_exceeded")
    except InsufficientFunds:
        await fsm.transition(db, job, fsm.FAILED, reason="insufficient_credits", actor="system")
        job.error_code = "insufficient_credits"
        job.error_message = "insufficient credits to authorize job"
        await record_audit(
            db, action="job.fail", outcome="failure", actor_user_id=current.id,
            ip=client_ip(request), resource_type="job", resource_id=str(job.id), reason="insufficient_credits",
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="insufficient_credits")
    await db.flush()
    return _shape_v2(job)
