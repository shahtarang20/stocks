"""BYOC routes (U8, docs/architecture/U8-byoc-design.md).

POST /v1/jobs/byoc                       submit a bring-your-own-container job (isolated by default, fail closed)
POST /v1/jobs/byoc/validate              dry-run the spec checks (no scan, nothing stored)
GET  /v1/admin/byoc/status               admin: allowlist, scanner, limits in effect
GET/POST /v1/admin/byoc/denylist         admin: known-bad image digests
DELETE   /v1/admin/byoc/denylist/{digest}

The submit flow mirrors POST /v1/jobs/v2 (same holds, spend limits, FSM); ledger, pricing and billing are untouched.
"""

from __future__ import annotations

import math
import re
import uuid
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.jobs import _assert_project_accessible
from app.api.routers.jobs_v2 import JobV2Out, _shape_v2, build_v2_plan
from app.core.config import settings
from app.db.session import get_db
from app.deps import client_ip, require_roles, require_verified_user
from app.models.byoc import ByocImageDenylist
from app.models.jobs import Job
from app.models.provider import ProviderMachine
from app.schemas.auth import CurrentUser
from app.services import byoc as byoc_svc
from app.services import image_scan
from app.services import job_fsm as fsm
from app.services import scheduler_v2
from app.services.audit import record_audit
from app.services.kits import KitError
from app.services.ledger import InsufficientFunds, lock_customer, to_minor
from app.services.scheduler import JobValidationError, schedule_job, validate_job
from app.services.spend_limits import SpendLimitExceeded, spend_guard

router = APIRouter(prefix="/v1/jobs", tags=["jobs-byoc"])
admin_router = APIRouter(prefix="/v1/admin/byoc", tags=["admin-byoc"])

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ByocJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: dict[str, Any]
    project_id: uuid.UUID | None = None
    machine_id: uuid.UUID | None = None
    priority: int = Field(default=0, ge=0, le=1_000_000)


def _http(exc: byoc_svc.ByocError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail={"errors": exc.errors})


@router.post("/byoc/validate")
async def validate_byoc(body: ByocJobCreate, current: CurrentUser = Depends(require_verified_user)) -> dict:
    try:
        spec = byoc_svc.validate_byoc_spec(body.spec)
    except byoc_svc.ByocError as exc:
        raise _http(exc)
    return {"ok": True, "image_digest": spec.digest, "isolation": spec.isolation,
            "effective_resources": byoc_svc.effective_resources(spec)}


@router.post("/byoc", response_model=JobV2Out, status_code=status.HTTP_201_CREATED)
async def create_byoc_job(
    body: ByocJobCreate,
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

    from app.services.abuse import assert_can_submit  # U16: no-op unless ABUSE_ENFORCE=true

    await assert_can_submit(db, current.id)
    await _assert_project_accessible(db, body.project_id, current)

    try:
        spec = byoc_svc.validate_byoc_spec(body.spec)
    except byoc_svc.ByocError as exc:
        raise _http(exc)

    if body.machine_id is not None:
        known = await db.scalar(
            select(ProviderMachine.id).where(ProviderMachine.id == body.machine_id, ProviderMachine.status != "deregistered")
        )
        if known is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="machine_not_found")

    try:
        await byoc_svc.ensure_image_cleared(db, spec.image, spec.digest)
        await db.commit()  # keep the scan result (clean or denied) even when the submit is refused below
    except byoc_svc.ByocError as exc:
        await db.commit()
        raise _http(exc)
    await lock_customer(db, current.id)  # the commit above released the transaction-scoped lock: take it again
    if idempotency_key:
        # ...and look again: a concurrent request with the same key may have created the job in that gap.
        prior = (
            await db.execute(select(Job).where(Job.customer_id == current.id, Job.idempotency_key == idempotency_key))
        ).scalar_one_or_none()
        if prior is not None:
            return _shape_v2(prior)

    block = byoc_svc.build_byoc_block(spec)
    try:
        plan = await build_v2_plan(
            db, current.id, SimpleNamespace(inputs=spec.inputs, outputs=[]),
            SimpleNamespace(slug="byoc", version="1", manifest={"outputs": []}),
        )
    except KitError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"errors": exc.errors})
    plan["outputs"] = [{"name": o.name, "path": o.path, "required": True} for o in spec.outputs]
    plan["egress"] = list(spec.network.egress)
    plan["byoc"] = block

    res = byoc_svc.effective_resources(spec)
    runtime_s = byoc_svc.max_runtime_seconds(spec)
    job = Job(
        customer_id=current.id,
        project_id=body.project_id,
        workload_type="byoc",
        model="byoc",
        input={},
        status=fsm.CREATED,
        gpu_count=res["gpu_count"],
        estimated_minutes=max(1, math.ceil(runtime_s / 60)),
        max_cost=spec.billing.max_cost,
        max_runtime_seconds=runtime_s,
        retry_policy={"max_retries": 0},
        priority=body.priority,
        execution_mode="hardware",
        isolation_tier="container",  # the byoc block forces gvisor / microvm on the agent; privacy tiers are refused
        required_gpu_runtime="cuda",  # ROCm is container-only in v1; BYOC never runs there
        network_policy=None,
        data_policy={"v2_plan": plan},
        idempotency_key=idempotency_key,
        machine_id=body.machine_id,
        spec_v2={
            "spec": spec.model_dump(mode="json"),
            "byoc": block,
            "effective_resources": res,
            "kit_image": spec.image,  # the claim reads the image from here
        },
    )
    db.add(job)
    await db.flush()

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
        ip=client_ip(request), resource_type="job", resource_id=str(job.id), reason="byoc",
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


# ---- admin ----------------------------------------------------------------------------------

class DenylistAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str = Field(min_length=71, max_length=71)
    reason: str = Field(min_length=3, max_length=1000)


def _row(r: ByocImageDenylist) -> dict:
    return {"digest": r.digest, "reason": r.reason, "created_at": r.created_at.isoformat() if r.created_at else None}


@admin_router.get("/status")
async def byoc_status(current: CurrentUser = Depends(require_roles("administrator"))) -> dict:
    scanner = image_scan.get_scanner()
    return {
        "registry_allowlist": byoc_svc.registry_allowlist(),
        "scanner": scanner.name,
        "scanner_configured": scanner.name != image_scan.UnconfiguredScanner.name,
        "allow_egress": settings.BYOC_ALLOW_EGRESS,
        "limits": {"gpus": settings.BYOC_MAX_GPUS, "cpus": settings.BYOC_MAX_CPUS,
                   "memory_mb": settings.BYOC_MAX_MEMORY_MB, "runtime_seconds": settings.BYOC_MAX_RUNTIME_SECONDS},
    }


@admin_router.get("/denylist")
async def list_denylist(
    current: CurrentUser = Depends(require_roles("administrator")), db: AsyncSession = Depends(get_db)
) -> dict:
    rows = (await db.execute(select(ByocImageDenylist).order_by(ByocImageDenylist.created_at.desc()).limit(500))).scalars().all()
    return {"items": [_row(r) for r in rows]}


@admin_router.post("/denylist", status_code=status.HTTP_201_CREATED)
async def add_denylist(
    body: DenylistAdd,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    digest = body.digest.strip().lower()
    if not _DIGEST.match(digest):
        raise HTTPException(status_code=422, detail="digest_must_be_sha256_hex")
    if await byoc_svc.is_denylisted(db, digest) is not None:
        raise HTTPException(status_code=409, detail="already_denylisted")
    row = ByocImageDenylist(digest=digest, reason=body.reason, added_by=current.id)
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="already_denylisted")
    await record_audit(
        db, action="byoc.denylist_add", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="byoc_image", resource_id=digest, reason=body.reason[:200],
    )
    await db.flush()
    return _row(row)


@admin_router.delete("/denylist/{digest}")
async def remove_denylist(
    digest: str,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    digest = digest.strip().lower()
    if not _DIGEST.match(digest):
        raise HTTPException(status_code=422, detail="digest_must_be_sha256_hex")
    row = await byoc_svc.is_denylisted(db, digest)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    await db.delete(row)
    await record_audit(
        db, action="byoc.denylist_remove", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="byoc_image", resource_id=digest,
    )
    await db.flush()
    return {"removed": digest}
