"""Fan-out jobs (U4, docs/architecture/U4-fan-out-design.md).

POST /v1/jobs/v2/fanout                                       submit one job split into parts
GET  /v1/fanout/{parent_job_id}                               group, parts and runs
POST /v1/fanout/{parent_job_id}/cancel                        cancel the parent and every live part
GET  /v1/fanout/{parent_job_id}/outputs/{name}/{index}/download   presigned link for one merged part file

Holds, scheduling, metering and settlement of every part are the existing functions (see services/fanout.py).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.jobs import _assert_project_accessible
from app.api.routers.jobs_v2 import JobV2Create
from app.db.session import get_db
from app.deps import client_ip, require_verified_user
from app.models.fanout import GROUP_COMPLETED, FanoutGroup, FanoutPart, FanoutPartRun
from app.models.jobs import Job
from app.models.provider import ProviderMachine
from app.schemas.auth import CurrentUser
from app.services import data_plane as dp
from app.services import fanout as svc
from app.services.audit import record_audit
from app.services.kits import KitError
from app.services.ledger import lock_customer

router = APIRouter(tags=["fanout"])


class FanoutCreate(JobV2Create):
    """U18: a private fan-out also carries one customer-made key set per part (see U18-privacy-modes-design.md)."""

    private_parts: list[dict] | None = None


class FanoutRunOut(BaseModel):
    job_id: uuid.UUID
    kind: str
    status: str


class FanoutPartOut(BaseModel):
    index: int
    status: str
    attempts: int
    weight: int
    winner_job_id: uuid.UUID | None = None
    duration_seconds: str | None = None
    final_cost: str | None = None
    error_code: str | None = None
    runs: list[FanoutRunOut] = []


class FanoutOut(BaseModel):
    parent_job_id: uuid.UUID
    group_id: uuid.UUID
    status: str  # group status: running | merging | completed | failed | cancelled
    job_status: str  # the parent job's FSM state, derived from the parts
    part_count: int
    counts: dict[str, int]
    error_code: str | None = None
    outputs: dict[str, Any] | None = None
    parts: list[FanoutPartOut] | None = None


async def _view(db: AsyncSession, group: FanoutGroup, parent: Job, include_parts: bool = True) -> FanoutOut:
    parts = list((await db.execute(
        select(FanoutPart).where(FanoutPart.group_id == group.id).order_by(FanoutPart.idx))).scalars().all())
    runs = list((await db.execute(
        select(FanoutPartRun).where(FanoutPartRun.part_id.in_([p.id for p in parts])).order_by(FanoutPartRun.created_at)
    )).scalars().all()) if parts else []
    by_part: dict[uuid.UUID, list[FanoutRunOut]] = {}
    for r in runs:
        by_part.setdefault(r.part_id, []).append(FanoutRunOut(job_id=r.job_id, kind=r.kind, status=r.status))
    counts: dict[str, int] = {}
    for p in parts:
        counts[p.status] = counts.get(p.status, 0) + 1
    return FanoutOut(
        parent_job_id=parent.id, group_id=group.id, status=group.status, job_status=parent.status,
        part_count=group.part_count, counts=counts, error_code=group.error_code,
        outputs=(group.merge_result or {}).get("fanout") if group.status == GROUP_COMPLETED else None,
        parts=[
            FanoutPartOut(
                index=p.idx, status=p.status, attempts=p.attempts, weight=int(p.weight), winner_job_id=p.winner_job_id,
                duration_seconds=str(p.duration_seconds) if p.duration_seconds is not None else None,
                final_cost=str(p.final_cost) if p.final_cost is not None else None,
                error_code=p.error_code, runs=by_part.get(p.id, []),
            ) for p in parts
        ] if include_parts else None,
    )


async def _owned(db: AsyncSession, user_id: uuid.UUID, parent_job_id: uuid.UUID, *, lock: bool = False):
    q = select(FanoutGroup).where(FanoutGroup.parent_job_id == parent_job_id, FanoutGroup.customer_id == user_id)
    if lock:
        q = q.with_for_update()
    group = (await db.execute(q)).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="fanout_not_found")
    parent = await db.get(Job, group.parent_job_id)
    if parent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="fanout_not_found")
    return group, parent


@router.post("/v1/jobs/v2/fanout", response_model=FanoutOut, status_code=status.HTTP_201_CREATED)
async def create_fanout(
    body: FanoutCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> FanoutOut:
    await lock_customer(db, current.id)

    if idempotency_key:
        prior = (await db.execute(
            select(Job).where(Job.customer_id == current.id, Job.idempotency_key == idempotency_key))).scalar_one_or_none()
        if prior is not None:
            group = (await db.execute(select(FanoutGroup).where(FanoutGroup.parent_job_id == prior.id))).scalar_one_or_none()
            if group is None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="idempotency_key_used_by_another_job")
            return await _view(db, group, prior)

    from app.services.abuse import assert_can_submit  # U16: no-op unless ABUSE_ENFORCE=true

    await assert_can_submit(db, current.id)

    try:
        dp.require_enabled()  # part outputs live in the data plane; without it a fan-out cannot merge
    except dp.DataPlaneError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.code)
    await _assert_project_accessible(db, body.project_id, current)
    if body.machine_id is not None:
        known = await db.scalar(
            select(ProviderMachine.id).where(ProviderMachine.id == body.machine_id, ProviderMachine.status != "deregistered"))
        if known is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="machine_not_found")

    try:
        parent, group = await svc.submit_fanout(
            db, customer_id=current.id, spec_raw=body.spec, project_id=body.project_id, machine_id=body.machine_id,
            priority=body.priority, isolation_tier=body.isolation_tier,
            required_gpu_runtime=body.required_gpu_runtime, idempotency_key=idempotency_key,
            private_parts=body.private_parts,
        )
    except (svc.FanoutError, KitError) as exc:
        raise HTTPException(status_code=exc.http_status, detail={"errors": exc.errors})

    await record_audit(
        db, action="job.create", outcome="success", actor_user_id=current.id, ip=client_ip(request),
        resource_type="job", resource_id=str(parent.id), reason="fanout",
    )
    # Start the first window now so the customer need not wait for the next sweep. If anything goes wrong here
    # the savepoint is rolled back and the worker sweep spawns the parts instead.
    try:
        async with db.begin_nested():
            await svc.advance_group(db, group)
    except Exception:
        svc.logger.exception("fanout: first window failed at submit; the worker sweep will start it")
    await db.flush()
    return await _view(db, group, parent)


@router.get("/v1/fanout/{parent_job_id}", response_model=FanoutOut)
async def get_fanout(
    parent_job_id: uuid.UUID,
    parts: bool = Query(default=True),
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> FanoutOut:
    group, parent = await _owned(db, current.id, parent_job_id)
    return await _view(db, group, parent, include_parts=parts)


@router.post("/v1/fanout/{parent_job_id}/cancel", response_model=FanoutOut)
async def cancel_fanout(
    parent_job_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> FanoutOut:
    await lock_customer(db, current.id)
    group, parent = await _owned(db, current.id, parent_job_id, lock=True)
    await svc.cancel_group(db, group)
    await record_audit(
        db, action="job.cancel", outcome="success", actor_user_id=current.id, ip=client_ip(request),
        resource_type="job", resource_id=str(parent.id), reason="fanout",
    )
    await db.flush()
    return await _view(db, group, parent)


@router.get("/v1/fanout/{parent_job_id}/outputs/{name}/{index}/download")
async def download_fanout_output(
    parent_job_id: uuid.UUID,
    name: str,
    index: int,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    group, _ = await _owned(db, current.id, parent_job_id)
    if group.status != GROUP_COMPLETED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="fanout_not_merged")
    entries = (((group.merge_result or {}).get("fanout") or {}).get("outputs") or {}).get(name)
    entry = next((e for e in entries or [] if e.get("index") == index), None)
    if entry is None or "file_id" not in entry:  # private groups have sealed outputs on the jobs, no download link
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="output_not_found")
    try:
        link = await dp.create_download_link(db, owner_id=current.id, file_id=uuid.UUID(entry["file_id"]), ip=client_ip(request))
    except dp.DataPlaneError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.code)
    return {
        "url": link["url"], "expires_at": link["expires_at"].isoformat(),
        "sha256": link["sha256"], "size_bytes": link["size_bytes"], "index": index, "name": name,
    }


@router.get("/v1/fanout/{parent_job_id}/privacy-receipt")
async def get_fanout_privacy_receipt(
    parent_job_id: uuid.UUID,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """U18: the signed job-level receipt (Merkle root of the ordered part receipt hashes). Owner only."""
    group, _ = await _owned(db, current.id, parent_job_id)
    receipt = (((group.merge_result or {}).get("fanout") or {}).get("job_receipt"))
    if group.status != GROUP_COMPLETED or not receipt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="fanout_privacy_receipt_not_available")
    return receipt
