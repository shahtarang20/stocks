"""Fan-out planner, part runner, straggler copies and merge (U4, docs/architecture/U4-fan-out-design.md).

One parent job is split into parts; each part is run by ordinary v2 child jobs. Holds, metering, settlement and
cancellation of a child use the existing functions unchanged (``schedule_job`` via the recovery sweep, ``settle``
on completion, ``billing.release_hold`` on cancel). Nothing here writes to the ledger.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import decimal
import hashlib
import json
import logging
import statistics
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.models.data_plane import OBJECT_READY, DataFile, DataObject
from app.models.fanout import (
    GROUP_ACTIVE,
    GROUP_CANCELLED,
    GROUP_COMPLETED,
    GROUP_FAILED,
    GROUP_MERGING,
    GROUP_RUNNING,
    PART_CANCELLED,
    PART_DONE,
    PART_FAILED,
    PART_PENDING,
    PART_RUNNING,
    RUN_ACTIVE,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_LOST,
    RUN_PRIMARY,
    RUN_RETRY,
    RUN_STRAGGLER,
    RUN_WON,
    FanoutGroup,
    FanoutPart,
    FanoutPartRun,
)
from app.models.jobs import Job
from app.schemas.job_spec_v2 import MAX_FANOUT_PARTS, NOT_SUPPORTED_YET, JobSpecV2
from app.services import billing
from app.services import fanout_privacy
from app.services import job_fsm as fsm
from app.services import scheduler_v2
from app.services.kits import KitError, assert_kit_intact, get_kit, validate_job_spec_v2

logger = logging.getLogger("computecoin.fanout")

MAX_ITEMS = 100_000
CENT = decimal.Decimal("0.01")
CHILD_TERMINAL_FAILED = (fsm.FAILED, fsm.TIMED_OUT, fsm.CANCELLED, fsm.DISPUTED)


def _stored_gpu_count(eff: dict) -> int:
    """jobs.gpu_count for a fan-out job or part. A CPU-only Kit (gpu_count 0) is refused unless CPU_ONLY_KITS_ENABLED."""
    try:
        return scheduler_v2.stored_gpu_count(eff)
    except scheduler_v2.CpuOnlyKitsDisabled:
        raise KitError("invalid_spec", 422, [{"field": "resources.gpu.count", "code": "cpu_only_kits_disabled",
                                              "message": "CPU-only Kits are not enabled on this platform"}])


class FanoutError(Exception):
    """Refusal at submit time; ``errors`` is a list of {field, code, message} like KitError."""

    def __init__(self, code: str, status: int = 422, errors: list[dict] | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = status
        self.errors = errors or [{"field": "parallelism", "code": code, "message": code}]


class FanoutMergeError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _err(field_: str, code: str, message: str | None = None) -> dict:
    return {"field": field_, "code": code, "message": message or code}


def max_parts() -> int:
    return max(1, min(int(settings.FANOUT_MAX_PARTS), MAX_FANOUT_PARTS))


# ---------------------------------------------------------------------------------------------
# Planner (pure: no database, no clock)
# ---------------------------------------------------------------------------------------------
@dataclass
class PartPlan:
    index: int
    patch: dict[str, Any]
    weight: int
    label: dict[str, Any] = field(default_factory=dict)


def plan_parts(*, manifest: dict, split: Any, params: dict, input_sizes: dict[str, int], limit: int | None = None) -> list[PartPlan]:
    """Cut one job into parts by the customer's ``split`` and the Kit's declared rule. Raises FanoutError.

    ``split`` is a SplitRange, SplitItems or SplitBytes. The Kit's manifest ``split`` says which param carries
    the slice; the customer may not set that param themselves (range and bytes).
    """
    limit = limit if limit is not None else max_parts()
    rules = manifest.get("split") or {}
    rule = rules.get(split.kind)
    if not isinstance(rule, dict) or "param" not in rule:
        raise FanoutError("kit_split_not_declared", 422, [
            _err("parallelism.split.kind", "kit_split_not_declared", f"this Kit does not declare a '{split.kind}' split")])
    param = rule["param"]
    plans: list[PartPlan] = []

    if split.kind == "range":
        if param in params:
            raise FanoutError("param_reserved_for_split", 422, [_err(f"params.{param}", "param_reserved_for_split")])
        count = -(-(split.to - split.from_ + 1) // split.chunk)
        if count > limit:
            raise FanoutError("too_many_parts", 422, [_err("parallelism.split.chunk", "too_many_parts", f"{count} parts, at most {limit}")])
        a = split.from_
        while a <= split.to:
            b = min(a + split.chunk - 1, split.to)
            plans.append(PartPlan(len(plans), {param: {"from": a, "to": b}}, b - a + 1, {"from": a, "to": b}))
            a = b + 1

    elif split.kind == "items":
        items = params.get(param)
        if not isinstance(items, list) or not items:
            raise FanoutError("items_param_missing", 422, [_err(f"params.{param}", "must_be_non_empty_list")])
        if len(items) > MAX_ITEMS:
            raise FanoutError("too_many_items", 422, [_err(f"params.{param}", "too_many_items", f"at most {MAX_ITEMS}")])
        count = -(-len(items) // split.chunk)
        if count > limit:
            raise FanoutError("too_many_parts", 422, [_err("parallelism.split.chunk", "too_many_parts", f"{count} parts, at most {limit}")])
        for i in range(0, len(items), split.chunk):
            chunk = items[i:i + split.chunk]
            plans.append(PartPlan(len(plans), {param: chunk}, len(chunk), {"count": len(chunk), "offset": i}))

    else:  # bytes
        if param in params:
            raise FanoutError("param_reserved_for_split", 422, [_err(f"params.{param}", "param_reserved_for_split")])
        size = input_sizes.get(split.input)
        if size is None or size <= 0:
            raise FanoutError("unknown_input", 422, [_err("parallelism.split.input", "unknown_input", "must name one of the job's inputs")])
        count = -(-size // split.chunk_bytes)
        if count > limit:
            raise FanoutError("too_many_parts", 422, [_err("parallelism.split.chunk_bytes", "too_many_parts", f"{count} parts, at most {limit}")])
        off = 0
        while off < size:
            n = min(split.chunk_bytes, size - off)
            plans.append(PartPlan(len(plans), {param: {"offset": off, "length": n}}, n, {"offset": off, "length": n}))
            off += n

    if not plans:
        raise FanoutError("empty_split", 422)
    return plans


def _share_cost(total: decimal.Decimal, weight: int, total_weight: int) -> decimal.Decimal:
    v = (decimal.Decimal(total) * weight / max(1, total_weight)).quantize(CENT, rounding=decimal.ROUND_UP)
    return max(v, CENT)


def child_raw_spec(base_spec: dict, part: Any, total_weight: int) -> dict:
    """The child's Job Spec v2: the parent's spec without ``parallelism``, the slice patched in, cost shares scaled."""
    spec = copy.deepcopy(base_spec)
    spec.pop("parallelism", None)
    params = dict(spec.get("params") or {})
    params.update(copy.deepcopy(part.patch))
    spec["params"] = params
    billing_ = dict(spec.get("billing") or {})
    if billing_.get("expected_units"):
        billing_["expected_units"] = max(1, round(int(billing_["expected_units"]) * int(part.weight) / max(1, total_weight)))
    if billing_.get("max_cost") is not None:
        billing_["max_cost"] = str(_share_cost(decimal.Decimal(str(billing_["max_cost"])), int(part.weight), total_weight))
    spec["billing"] = billing_
    return spec


# ---------------------------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------------------------
async def submit_fanout(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    spec_raw: Any,
    project_id: uuid.UUID | None,
    machine_id: uuid.UUID | None,
    priority: int,
    isolation_tier: str,
    required_gpu_runtime: str,
    idempotency_key: str | None,
    private_parts: Any = None,
    require_dedicated_host: bool = False,
) -> tuple[Job, FanoutGroup]:
    """Validate, plan and persist a fan-out: parent job (RUNNING), group and part rows. No child job yet.

    Raises FanoutError or KitError (both carry field-level ``errors``). The caller commits.
    """
    from pydantic import ValidationError

    from app.api.routers.jobs_v2 import build_v2_plan
    from app.services.kits import _pydantic_errors

    is_private = isolation_tier in fanout_privacy.PRIVATE_TIERS
    if is_private:  # U18: fail closed unless the owner turned it on; the platform never splits or sees private input
        if not fanout_privacy.private_fanout_enabled():
            raise FanoutError("privacy_tier_not_supported_for_fanout", 422, [_err("isolation_tier", "privacy_tier_not_supported_for_fanout")])
    elif isolation_tier not in ("container", "open"):
        raise FanoutError("privacy_tier_not_supported_for_fanout", 422, [_err("isolation_tier", "privacy_tier_not_supported_for_fanout")])
    elif private_parts is not None:
        raise FanoutError("private_parts_need_private_tier", 422, [_err("private_parts", "private_parts_need_private_tier")])
    if not isinstance(spec_raw, dict):
        raise FanoutError("invalid_spec", 422, [_err("spec", "must_be_object")])
    errors = [_err(k, "not_supported_yet", f"'{k}' is not available in this version") for k in NOT_SUPPORTED_YET if k in spec_raw]
    try:
        spec = JobSpecV2.model_validate({k: v for k, v in spec_raw.items() if k not in NOT_SUPPORTED_YET})
    except ValidationError as exc:
        raise FanoutError("invalid_spec", 422, errors + _pydantic_errors(exc))
    if errors:
        raise FanoutError("invalid_spec", 422, errors)
    if spec.parallelism is None:
        raise FanoutError("parallelism_required", 422, [_err("parallelism", "required", "a fan-out job needs 'parallelism'")])
    par = spec.parallelism

    slug, version = spec.kit_slug_version()
    kit = await get_kit(db, slug, version)
    if kit is None or kit.status != "published":
        raise KitError("invalid_spec", 422, [_err("kit", "kit_not_found_or_unavailable")])
    assert_kit_intact(kit)

    v2_plan = await build_v2_plan(db, customer_id, spec, kit)  # inputs must be the customer's own live files
    sizes = {i["name"]: int(i["size_bytes"]) for i in v2_plan["inputs"]}
    plans = plan_parts(manifest=kit.manifest, split=par.split, params=spec.params, input_sizes=sizes)
    total_weight = sum(p.weight for p in plans)
    out_names = [o["name"] for o in v2_plan["outputs"] if o.get("required")]
    private_plan: dict | None = None
    if is_private:
        if par.split.kind == "bytes" or spec.inputs:
            raise FanoutError("private_fanout_needs_encrypted_inputs", 422, [_err(
                "parallelism" if par.split.kind == "bytes" else "inputs", "private_fanout_needs_encrypted_inputs",
                "private parts carry their input inside encrypted_input; data-plane inputs and byte splits are plaintext")])
        try:
            keys = fanout_privacy.validate_private_parts(private_parts, len(plans))
        except fanout_privacy.PrivateFanoutError as exc:
            raise FanoutError(exc.code, 422, exc.errors) from None
        private_plan = {"tier": isolation_tier, "keys": keys}
        out_names = []  # outputs are sealed on each job, not data-plane files

    base_spec = spec.model_dump(mode="json", by_alias=True)
    probe_parts = [SimpleNamespace(patch=p.patch, weight=p.weight) for p in plans]
    first = None
    for i, p in enumerate(probe_parts):  # every part is checked now; nothing is created if one is refused
        try:
            validated = await validate_job_spec_v2(db, child_raw_spec(base_spec, p, total_weight))
        except KitError as exc:
            raise KitError(exc.code, exc.http_status, [{**e, "field": f"parts[{i}].{e.get('field', '')}"} for e in exc.errors])
        first = first or validated
    eff = first.effective_resources
    gpu_count = _stored_gpu_count(eff)

    parent = Job(
        customer_id=customer_id, project_id=project_id, workload_type="kit_fanout", model=f"{kit.slug}@{kit.version}",
        input={}, status=fsm.CREATED, gpu_count=gpu_count,
        estimated_minutes=scheduler_v2.estimate_minutes(kit.manifest, spec.billing.expected_units, gpu_count),
        max_cost=spec.billing.max_cost, max_runtime_seconds=settings.JOB_MAX_RUNTIME_DEFAULT,
        retry_policy={"max_retries": 0}, priority=priority, execution_mode="hardware", isolation_tier=isolation_tier,
        required_gpu_runtime=required_gpu_runtime.lower(), network_policy=None,
        data_policy={"fanout": True, **({"require_dedicated_host": True} if require_dedicated_host else {})},
        idempotency_key=idempotency_key, machine_id=machine_id,
        spec_v2={"spec": base_spec, "effective_resources": eff, "kit_manifest_sha256": kit.manifest_sha256,
                 "kit_image": kit.image_digest},
        kit_id=kit.slug, kit_version=kit.version,
    )
    db.add(parent)
    await db.flush()
    # The parent is never scheduled and holds nothing. It walks the normal FSM to RUNNING (lease NULL, so the lease
    # recovery skips it) and ends COMPLETED / FAILED / CANCELLED from the part states.
    for state in (fsm.VALIDATING, fsm.QUEUED, fsm.MATCHED, fsm.RESERVED, fsm.RUNNING):
        await fsm.transition(db, parent, state, reason="fanout_planned", actor="system")

    group = FanoutGroup(
        parent_job_id=parent.id, customer_id=customer_id, status=GROUP_RUNNING, part_count=len(plans),
        split=par.split.model_dump(mode="json", by_alias=True),
        plan={
            "outputs": out_names,
            "max_parallel": int(par.max_parallel or settings.FANOUT_DEFAULT_MAX_PARALLEL),
            "straggler_multiple": float(par.straggler_multiple if par.straggler_multiple is not None else settings.FANOUT_STRAGGLER_MULTIPLE),
            "max_part_attempts": int(par.max_part_attempts or settings.FANOUT_MAX_PART_ATTEMPTS),
            "total_weight": total_weight,
            **({"private": private_plan} if private_plan else {}),
        },
    )
    db.add(group)
    await db.flush()
    for p in plans:
        db.add(FanoutPart(group_id=group.id, idx=p.index, patch=p.patch, weight=p.weight, status=PART_PENDING))
    await db.flush()
    return parent, group


# ---------------------------------------------------------------------------------------------
# Child jobs
# ---------------------------------------------------------------------------------------------
def part_data_policy(parent: Job, v2_plan: dict, private_keys: dict | None) -> dict:
    """The ``data_policy`` of one part (or one straggler copy) of a fan-out.

    * A PRIVATE part (``private_keys`` given, U18) carries only the Kit and the output names/paths (from the signed
      manifest). No input, asset, egress or checkpoint entry: inputs travel inside ``encrypted_input`` and a private job
      never gets network.
    * Otherwise it carries the full plan.
    * If the customer asked for dedicated computers only, the parent records it and EVERY part and straggler copy
      honours it (a part must never land on a shared PC the customer ruled out)."""
    if private_keys is not None:
        policy: dict = {
            "result_public_key": private_keys["result_public_key"], "private_fanout": True,
            "v2_plan": {"kit": v2_plan["kit"], "outputs": v2_plan["outputs"]},
        }
    else:
        policy = {"v2_plan": v2_plan}
    if (parent.data_policy or {}).get("require_dedicated_host") is True:
        policy["require_dedicated_host"] = True
    return policy


async def _spawn_run(db: AsyncSession, group: FanoutGroup, parent: Job, part: FanoutPart, kind: str) -> FanoutPartRun:
    """Create one ordinary v2 child job for ``part`` (QUEUED; the existing recovery sweep schedules and holds it)."""
    from app.api.routers.jobs_v2 import build_v2_plan
    from app.services.scheduler import JobValidationError, validate_job

    raw = child_raw_spec((parent.spec_v2 or {}).get("spec") or {}, part, int(group.plan.get("total_weight") or 1))
    private = (group.plan or {}).get("private")
    pkeys = private["keys"][part.idx] if private else None
    validated = await validate_job_spec_v2(db, raw)
    spec, kit, eff = validated.spec, validated.kit, validated.effective_resources
    v2_plan = await build_v2_plan(db, group.customer_id, spec, kit)
    gpu_count = _stored_gpu_count(eff)
    child = Job(
        customer_id=group.customer_id, project_id=parent.project_id, workload_type="kit",
        model=f"{kit.slug}@{kit.version}", input={}, status=fsm.CREATED, gpu_count=gpu_count,
        estimated_minutes=scheduler_v2.estimate_minutes(kit.manifest, spec.billing.expected_units, gpu_count),
        max_cost=spec.billing.max_cost, max_runtime_seconds=settings.JOB_MAX_RUNTIME_DEFAULT,
        retry_policy={"max_retries": 0 if private else max(0, int(settings.FANOUT_CHILD_MAX_RETRIES))},
        priority=parent.priority, execution_mode="hardware", isolation_tier=parent.isolation_tier,
        required_gpu_runtime=parent.required_gpu_runtime, network_policy=None,
        data_policy=part_data_policy(parent, v2_plan, pkeys if private else None),
        encrypted_input=pkeys["encrypted_input"] if private else None,
        dk_shares=pkeys["dk_shares"] if private else None, idempotency_key=None,
        machine_id=None,  # a straggler copy must be free to land on a different host than the slow one
        spec_v2={"spec": spec.model_dump(mode="json"), "effective_resources": eff,
                 "kit_manifest_sha256": kit.manifest_sha256, "kit_image": kit.image_digest},
        kit_id=kit.slug, kit_version=kit.version,
    )
    db.add(child)
    await db.flush()
    await fsm.transition(db, child, fsm.VALIDATING, reason=f"fanout_part_{part.idx}", actor="system")
    try:
        await validate_job(db, child)
    except JobValidationError as exc:
        await fsm.transition(db, child, fsm.FAILED, reason=exc.code, actor="system")
        child.error_code = exc.code
        child.error_message = f"validation failed: {exc.code}"
    else:
        await fsm.transition(db, child, fsm.QUEUED, reason="validated", actor="system")
    run = FanoutPartRun(part_id=part.id, job_id=child.id, kind=kind, status=RUN_ACTIVE)
    db.add(run)
    await db.flush()
    return run


async def _cancel_child(db: AsyncSession, job: Job) -> None:
    """Cancel a child exactly as ``POST /v1/jobs/{id}/cancel`` does: free capacity, CANCELLED, release the whole hold."""
    from app.services.leases import release_job_reservation

    if job.status not in fsm.CANCELLABLE_STATES:
        return
    job.cancel_requested = True
    await release_job_reservation(db, job, utcnow())
    await fsm.transition(db, job, fsm.CANCELLED, reason="fanout_cancel", actor="system")
    await billing.release_hold(db, job)  # a cancelled job is never charged


# ---------------------------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------------------------
async def _output_file(db: AsyncSession, customer_id: uuid.UUID, job_id: uuid.UUID, name: str) -> DataFile | None:
    f = (
        await db.execute(
            select(DataFile)
            .where(DataFile.owner_id == customer_id, DataFile.name == f"out/{job_id}/{name}", DataFile.deleted_at.is_(None))
            .order_by(DataFile.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if f is None or f.object_id is None:
        return None
    if f.expires_at is not None and ensure_aware(f.expires_at) <= utcnow():
        return None
    return f


def _store():
    from app.services import data_plane as dp

    try:
        return dp.require_enabled()
    except dp.DataPlaneError as exc:
        raise FanoutMergeError(exc.code) from None


async def _head(store: Any, key: str) -> int | None:
    try:
        return await asyncio.to_thread(store.head_size, key)
    except Exception:
        logger.exception("fanout: store head failed")
        raise FanoutMergeError("store_unavailable") from None


async def _valid_outputs(db: AsyncSession, store: Any, group: FanoutGroup, job: Job) -> bool:
    """A COMPLETED child is a valid result when every required output file exists with the recorded size."""
    if fanout_privacy.is_private_group(group.plan):
        return await fanout_privacy.private_run_valid(db, job)
    for name in group.plan.get("outputs") or []:
        f = await _output_file(db, group.customer_id, job.id, name)
        if f is None:
            return False
        obj = await db.get(DataObject, f.object_id)
        if obj is None or obj.status != OBJECT_READY:
            return False
        size = await _head(store, obj.storage_key)
        if size is None or int(size) != int(f.size_bytes):
            return False
    return True


def _run_seconds(job: Job) -> decimal.Decimal | None:
    raw = (job.metrics or {}).get("duration_seconds")
    try:
        if raw is not None:
            return decimal.Decimal(str(raw))
    except (decimal.InvalidOperation, ValueError):
        pass
    if job.started_at is not None and job.finished_at is not None:
        return decimal.Decimal(str((ensure_aware(job.finished_at) - ensure_aware(job.started_at)).total_seconds()))
    return None


def _part_label(part: FanoutPart) -> dict:
    """What the part covered: the range or byte window, or the item count and weight."""
    for v in (part.patch or {}).values():
        if isinstance(v, dict):
            return v
    return {"weight": int(part.weight)}


async def merge_group(db: AsyncSession, group: FanoutGroup, parts: list[FanoutPart]) -> dict:
    """Collect the winners' output files in part order and re-check each one. Raises FanoutMergeError.

    Result (stored at ``jobs.output`` and ``group.merge_result``) lists, per output name, one entry per part in
    index order. Bytes are not combined: the data plane has no compose call and no merge Kit exists yet.
    """
    if fanout_privacy.is_private_group(group.plan):
        parent = await db.get(Job, group.parent_job_id)
        try:
            return await fanout_privacy.merge_private(db, group, parts, parent)
        except fanout_privacy.MergeFailure as exc:
            raise FanoutMergeError(exc.code) from None
    store = _store()
    names = list(group.plan.get("outputs") or [])
    out: dict[str, list[dict]] = {n: [] for n in names}
    inline_max = int(settings.DATA_PLANE_INLINE_VERIFY_MAX_BYTES)
    for part in sorted(parts, key=lambda p: p.idx):
        if part.status != PART_DONE or part.winner_job_id is None:
            raise FanoutMergeError("part_not_done")
        for name in names:
            f = await _output_file(db, group.customer_id, part.winner_job_id, name)
            obj = await db.get(DataObject, f.object_id) if f is not None else None
            if f is None or obj is None or obj.status != OBJECT_READY:
                raise FanoutMergeError("output_missing")
            size = await _head(store, obj.storage_key)
            if size is None or int(size) != int(f.size_bytes):
                raise FanoutMergeError("output_size_mismatch")
            if int(f.size_bytes) <= inline_max:
                try:
                    digest = await asyncio.to_thread(store.sha256_of, obj.storage_key)
                except Exception:
                    logger.exception("fanout: store hash failed")
                    raise FanoutMergeError("store_unavailable") from None
                if str(digest).lower() != str(f.sha256).lower():
                    raise FanoutMergeError("output_hash_mismatch")
            out[name].append({
                "index": part.idx, "file_id": str(f.id), "sha256": f.sha256, "size_bytes": int(f.size_bytes),
                "part": _part_label(part),
            })
    manifest_sha = hashlib.sha256(json.dumps(out, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"fanout": {"parts": len(parts), "merge": "ordered_manifest", "outputs": out, "manifest_sha256": manifest_sha}}


# ---------------------------------------------------------------------------------------------
# Advance one group (called by the worker sweep, once per group per transaction)
# ---------------------------------------------------------------------------------------------
async def _load(db: AsyncSession, group: FanoutGroup):
    parts = list((await db.execute(
        select(FanoutPart).where(FanoutPart.group_id == group.id).order_by(FanoutPart.idx))).scalars().all())
    ids = [p.id for p in parts]
    runs = list((await db.execute(
        select(FanoutPartRun).where(FanoutPartRun.part_id.in_(ids)).order_by(FanoutPartRun.created_at))).scalars().all()) if ids else []
    job_ids = [r.job_id for r in runs]
    jobs = {j.id: j for j in (await db.execute(select(Job).where(Job.id.in_(job_ids)))).scalars().all()} if job_ids else {}
    return parts, runs, jobs


async def _finish(db: AsyncSession, group: FanoutGroup, parent: Job, parts: list[FanoutPart], runs: list[FanoutPartRun],
                  jobs: dict, *, status: str, error_code: str | None, reason: str) -> None:
    """Cancel everything still alive, then settle the group and the parent in their terminal states."""
    for r in runs:
        if r.status == RUN_ACTIVE:
            j = jobs.get(r.job_id)
            if j is not None:
                await _cancel_child(db, j)
            r.status = RUN_CANCELLED
    for p in parts:
        if p.status in (PART_PENDING, PART_RUNNING):
            p.status = PART_CANCELLED
    group.status = status
    group.error_code = error_code
    group.finished_at = utcnow()
    if parent is not None and parent.status in fsm.CANCELLABLE_STATES:
        target = fsm.CANCELLED if status == GROUP_CANCELLED else fsm.FAILED
        if status == GROUP_CANCELLED:
            parent.cancel_requested = True
        await fsm.transition(db, parent, target, reason=reason, actor="system")
        if target == fsm.FAILED:
            parent.error_code = error_code
            parent.error_message = f"fan-out failed: {error_code}"[:1024]
    await db.flush()


async def cancel_group(db: AsyncSession, group: FanoutGroup) -> None:
    """Cancel the parent and every live part run (customer cancel). Idempotent for a finished group."""
    if group.status not in GROUP_ACTIVE:
        return
    parent = await db.get(Job, group.parent_job_id)
    parts, runs, jobs = await _load(db, group)
    await _finish(db, group, parent, parts, runs, jobs, status=GROUP_CANCELLED, error_code=None, reason="customer_cancel")


async def advance_group(db: AsyncSession, group: FanoutGroup, now: dt.datetime | None = None) -> dict:
    """One pass: refresh runs, pick winners, re-run failed parts alone, spawn within the window, copy stragglers, merge."""
    now = now or utcnow()
    stats = {"spawned": 0, "retried": 0, "stragglers": 0, "won": 0, "cancelled_losers": 0, "merged": 0}
    if group.status not in GROUP_ACTIVE:
        return stats
    parent = await db.get(Job, group.parent_job_id)
    parts, runs, jobs = await _load(db, group)
    if parent is None:
        await _finish(db, group, parent, parts, runs, jobs, status=GROUP_FAILED, error_code="parent_missing", reason="parent_missing")
        return stats

    # Parent cancelled or ended by someone else (for example the v1 cancel route): cancel the parts.
    if parent.status in (fsm.CANCELLED, fsm.FAILED, fsm.TIMED_OUT) or parent.cancel_requested:
        await _finish(db, group, parent, parts, runs, jobs, status=GROUP_CANCELLED, error_code=None, reason="parent_cancelled")
        return stats

    plan = group.plan or {}
    max_attempts = int(plan.get("max_part_attempts") or settings.FANOUT_MAX_PART_ATTEMPTS)
    max_parallel = int(plan.get("max_parallel") or settings.FANOUT_DEFAULT_MAX_PARALLEL)
    runs_by_part: dict[uuid.UUID, list[FanoutPartRun]] = {}
    for r in runs:
        runs_by_part.setdefault(r.part_id, []).append(r)

    if group.status == GROUP_RUNNING:
        store = None
        for part in parts:
            if part.status in (PART_DONE, PART_FAILED, PART_CANCELLED):
                continue
            part_runs = runs_by_part.get(part.id, [])
            active = [r for r in part_runs if r.status == RUN_ACTIVE]
            completed = sorted(
                (r for r in active if jobs[r.job_id].status == fsm.COMPLETED),
                key=lambda r: ensure_aware(jobs[r.job_id].finished_at) or now,
            )
            for r in active:  # terminal-but-not-completed children are failed runs
                if jobs[r.job_id].status in CHILD_TERMINAL_FAILED:
                    r.status = RUN_FAILED
                    part.error_code = jobs[r.job_id].error_code or "run_failed"
            winner = None
            for r in completed:
                try:
                    store = store or (None if fanout_privacy.is_private_group(group.plan) else _store())
                    ok = await _valid_outputs(db, store, group, jobs[r.job_id])
                except FanoutMergeError:
                    ok = None  # store unavailable: leave the run active and look again next sweep
                if ok is None:
                    break
                if ok:
                    winner = r
                    break
                r.status = RUN_FAILED
                part.error_code = "output_missing"
            if winner is not None:
                wj = jobs[winner.job_id]
                winner.status = RUN_WON
                part.status = PART_DONE
                part.winner_job_id = wj.id
                part.duration_seconds = _run_seconds(wj)
                part.final_cost = wj.final_cost
                part.error_code = None
                stats["won"] += 1
                for r in part_runs:  # first valid result wins; the other copy is cancelled and its hold released
                    if r is not winner and r.status == RUN_ACTIVE:
                        await _cancel_child(db, jobs[r.job_id])
                        r.status = RUN_LOST
                        stats["cancelled_losers"] += 1
                continue
            if any(r.status == RUN_ACTIVE for r in part_runs):
                part.status = PART_RUNNING
            elif part.attempts >= max_attempts:
                part.status = PART_FAILED
                part.error_code = part.error_code or "run_failed"
            else:
                part.status = PART_PENDING  # re-run alone: only this part gets a new child

        failed = [p for p in parts if p.status == PART_FAILED]
        if failed:
            await _finish(db, group, parent, parts, runs, jobs, status=GROUP_FAILED, error_code="fanout_part_failed", reason="part_failed")
            return stats

        # Spawn within the window, lowest index first (retries wait their turn like any part).
        running = sum(1 for p in parts if p.status == PART_RUNNING)
        for part in parts:
            if running >= max_parallel:
                break
            if part.status != PART_PENDING:
                continue
            kind = RUN_PRIMARY if part.attempts == 0 else RUN_RETRY
            try:
                run = await _spawn_run(db, group, parent, part, kind)
            except KitError as exc:
                part.status = PART_FAILED
                part.error_code = exc.code
                await _finish(db, group, parent, parts, runs, jobs, status=GROUP_FAILED, error_code="fanout_part_failed", reason="spawn_failed")
                return stats
            runs.append(run)
            runs_by_part.setdefault(part.id, []).append(run)
            jobs[run.job_id] = await db.get(Job, run.job_id)
            part.attempts += 1
            part.status = PART_RUNNING
            running += 1
            stats["spawned" if kind == RUN_PRIMARY else "retried"] += 1

        # Straggler copies.
        multiple = float(plan.get("straggler_multiple") or 0)
        done_secs = [float(p.duration_seconds) for p in parts if p.status == PART_DONE and p.duration_seconds is not None]
        if multiple > 0 and not fanout_privacy.is_private_group(plan) and len(done_secs) >= max(1, int(settings.FANOUT_STRAGGLER_MIN_DONE)):
            threshold = multiple * statistics.median(done_secs)
            live_copies = sum(1 for r in runs if r.kind == RUN_STRAGGLER and r.status == RUN_ACTIVE)
            cap = max(1, len(parts) // 10)
            for part in parts:
                if live_copies >= cap:
                    break
                active = [r for r in runs_by_part.get(part.id, []) if r.status == RUN_ACTIVE]
                if part.status != PART_RUNNING or len(active) != 1 or active[0].kind == RUN_STRAGGLER:
                    continue
                if any(r.kind == RUN_STRAGGLER for r in runs_by_part.get(part.id, [])):
                    continue  # at most one copy per part, ever
                j = jobs[active[0].job_id]
                if j.status != fsm.RUNNING or j.started_at is None:
                    continue
                if (now - ensure_aware(j.started_at)).total_seconds() <= threshold:
                    continue
                try:
                    run = await _spawn_run(db, group, parent, part, RUN_STRAGGLER)
                except KitError:
                    continue  # a failed copy attempt never harms the original run
                runs.append(run)
                runs_by_part.setdefault(part.id, []).append(run)
                jobs[run.job_id] = await db.get(Job, run.job_id)
                live_copies += 1
                stats["stragglers"] += 1

        if all(p.status == PART_DONE for p in parts):
            group.status = GROUP_MERGING

    if group.status == GROUP_MERGING:
        try:
            result = await merge_group(db, group, parts)
        except FanoutMergeError as exc:
            group.merge_attempts += 1
            logger.warning("fanout %s merge attempt %s failed: %s", group.id, group.merge_attempts, exc.code)
            if group.merge_attempts >= int(settings.FANOUT_MERGE_MAX_ATTEMPTS):
                await _finish(db, group, parent, parts, runs, jobs, status=GROUP_FAILED, error_code="fanout_merge_failed", reason=exc.code)
            return stats
        group.merge_result = result
        group.status = GROUP_COMPLETED
        group.finished_at = utcnow()
        parent.output = result
        parent.final_cost = sum((p.final_cost or decimal.Decimal(0) for p in parts), decimal.Decimal(0))
        await fsm.transition(db, parent, fsm.VERIFYING, reason="fanout_merged", actor="system")
        await fsm.transition(db, parent, fsm.COMPLETED, reason="fanout_completed", actor="system")
        stats["merged"] = 1
    await db.flush()
    return stats


async def sweep_fanouts(sm: Any) -> dict:
    """Worker step. One transaction and one try/except per group: a broken group never blocks the others."""
    totals: dict[str, int] = {"groups": 0, "spawned": 0, "retried": 0, "stragglers": 0, "won": 0,
                              "cancelled_losers": 0, "merged": 0, "errors": 0}
    async with sm() as db:
        ids = list((await db.execute(
            select(FanoutGroup.id).where(FanoutGroup.status.in_(GROUP_ACTIVE)).order_by(FanoutGroup.created_at).limit(200)
        )).scalars().all())
    for gid in ids:
        try:
            async with sm() as db:
                group = (await db.execute(
                    select(FanoutGroup).where(FanoutGroup.id == gid).with_for_update(skip_locked=True)
                )).scalar_one_or_none()
                if group is None or group.status not in GROUP_ACTIVE:
                    continue
                s = await advance_group(db, group)
                await db.commit()
            totals["groups"] += 1
            for k, v in s.items():
                totals[k] = totals.get(k, 0) + v
        except Exception:
            logger.exception("fanout sweep failed for group %s (will retry next sweep)", gid)
            totals["errors"] += 1
    return totals
