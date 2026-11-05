"""Pipelines (U9, docs/architecture/U9-pipelines-design.md).

POST /v1/pipelines                    submit a DAG of Kit jobs / fan-outs linked by named output -> input files
GET  /v1/pipelines/{id}               run, steps, outputs
POST /v1/pipelines/{id}/cancel        cancel running steps, mark waiting steps cancelled
POST /v1/pipelines/{id}/restart       restart a failed run from its failed step, reusing succeeded steps' outputs

Every step is an ordinary job; holds, scheduling, metering and settlement are the existing functions.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.jobs import _assert_project_accessible
from app.db.session import get_db
from app.deps import client_ip, require_verified_user
from app.models.pipelines import PipelineRun
from app.schemas.auth import CurrentUser
from app.services import data_plane as dp
from app.services import pipelines as svc
from app.services.audit import record_audit
from app.services.ledger import lock_customer

router = APIRouter(prefix="/v1/pipelines", tags=["pipelines"])


class PipelineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: dict[str, Any]
    project_id: uuid.UUID | None = None
    priority: int = Field(default=0, ge=0, le=1_000_000)
    # Privacy tiers are refused (fail closed); U18 adds them.
    isolation_tier: str = Field(default="container", pattern="^(container|open|gvisor|microvm|sealed|sealed_pro|confidential)$")
    required_gpu_runtime: str = Field(default="cuda", pattern="^(cuda|rocm|any)$")


class StepOut(BaseModel):
    id: str
    kind: str
    status: str
    job_id: uuid.UUID | None = None
    attempts: int
    needs: list[dict]
    outputs: dict | None = None
    error_code: str | None = None
    history: list[dict] = []


class PipelineOut(BaseModel):
    id: uuid.UUID
    name: str | None = None
    status: str
    restart_count: int
    error_code: str | None = None
    steps: list[StepOut]
    outputs: dict | None = None
    restart: dict | None = None


async def _view(db: AsyncSession, run: PipelineRun, restart: dict | None = None) -> PipelineOut:
    steps = await svc.load_steps(db, run)
    return PipelineOut(
        id=run.id, name=run.name, status=run.status, restart_count=int(run.restart_count or 0),
        error_code=run.error_code, outputs=run.outputs, restart=restart,
        steps=[
            StepOut(
                id=s.step_key, kind=s.kind, status=s.status, job_id=s.job_id, attempts=int(s.attempts or 0),
                needs=list(s.needs or []), outputs=s.outputs, error_code=s.error_code, history=list(s.history or []),
            ) for s in steps
        ],
    )


async def _owned(db: AsyncSession, user_id: uuid.UUID, run_id: uuid.UUID, *, lock: bool = False) -> PipelineRun:
    q = select(PipelineRun).where(PipelineRun.id == run_id, PipelineRun.customer_id == user_id)
    if lock:
        q = q.with_for_update()
    run = (await db.execute(q)).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pipeline_not_found")
    return run


def _refuse(exc: svc.PipelineError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail={"errors": exc.errors})


@router.post("", response_model=PipelineOut, status_code=status.HTTP_201_CREATED)
async def create_pipeline(
    body: PipelineCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> PipelineOut:
    await lock_customer(db, current.id)

    if idempotency_key:
        prior = (await db.execute(select(PipelineRun).where(
            PipelineRun.customer_id == current.id, PipelineRun.idempotency_key == idempotency_key
        ).limit(1))).scalars().first()
        if prior is not None:
            return await _view(db, prior)

    if body.isolation_tier not in ("container", "open"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="privacy_tier_not_supported_for_pipelines")
    try:
        dp.require_enabled()  # step outputs live in the data plane; without it nothing can be handed on
    except dp.DataPlaneError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.code)

    from app.services.abuse import assert_can_submit  # U16: no-op unless ABUSE_ENFORCE=true

    await assert_can_submit(db, current.id)
    await _assert_project_accessible(db, body.project_id, current)
    try:
        run = await svc.create_run(
            db, customer_id=current.id, raw=body.spec, project_id=body.project_id, priority=body.priority,
            isolation_tier=body.isolation_tier, required_gpu_runtime=body.required_gpu_runtime,
            idempotency_key=idempotency_key,
        )
    except svc.PipelineError as exc:
        raise _refuse(exc)
    await record_audit(
        db, action="pipeline.create", outcome="success", actor_user_id=current.id, ip=client_ip(request),
        resource_type="pipeline", resource_id=str(run.id),
    )
    # Start the first steps now; if that fails the savepoint is rolled back and the worker sweep starts them.
    try:
        async with db.begin_nested():
            await svc.advance_run(db, run)
    except Exception:
        svc.logger.exception("pipelines: first pass failed at submit; the worker sweep will start it")
    await db.flush()
    return await _view(db, run)


@router.get("/{pipeline_id}", response_model=PipelineOut)
async def get_pipeline(
    pipeline_id: uuid.UUID,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> PipelineOut:
    return await _view(db, await _owned(db, current.id, pipeline_id))


@router.post("/{pipeline_id}/cancel", response_model=PipelineOut)
async def cancel_pipeline(
    pipeline_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> PipelineOut:
    await lock_customer(db, current.id)
    run = await _owned(db, current.id, pipeline_id, lock=True)
    await svc.cancel_run(db, run)
    await record_audit(
        db, action="pipeline.cancel", outcome="success", actor_user_id=current.id, ip=client_ip(request),
        resource_type="pipeline", resource_id=str(run.id),
    )
    await db.flush()
    return await _view(db, run)


@router.post("/{pipeline_id}/restart", response_model=PipelineOut)
async def restart_pipeline(
    pipeline_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> PipelineOut:
    await lock_customer(db, current.id)
    run = await _owned(db, current.id, pipeline_id, lock=True)
    try:
        info = await svc.restart_run(db, run)
    except svc.PipelineError as exc:
        raise _refuse(exc)
    await record_audit(
        db, action="pipeline.restart", outcome="success", actor_user_id=current.id, ip=client_ip(request),
        resource_type="pipeline", resource_id=str(run.id), reason=",".join(info["restarted_steps"])[:200],
    )
    try:
        async with db.begin_nested():
            await svc.advance_run(db, run)
    except Exception:
        svc.logger.exception("pipelines: first pass failed at restart; the worker sweep will start it")
    await db.flush()
    return await _view(db, run, restart=info)
