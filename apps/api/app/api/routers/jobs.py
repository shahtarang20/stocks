"""Customer job endpoints (customer JWT, owner-scoped).

POST /v1/jobs drives the synchronous pipeline CREATED -> VALIDATING -> QUEUED ->
MATCHED -> RESERVED inline (a background worker is unnecessary at MVP scale; the
same service functions are reused by the lease sweeper). A customer only ever sees
or cancels their own jobs/logs/artifacts (others -> 404).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.db.session import get_db
from app.deps import client_ip, get_current_user, require_verified_user
from app.models.provider import ProviderMachine
from app.models.jobs import Artifact, Job, JobEvent, JobLogMetadata
from app.models.org import OrganizationMember, Project
from app.schemas.auth import CurrentUser
from app.schemas.jobs import (
    ArtifactDownloadOut,
    ArtifactOut,
    JobCancelOut,
    JobCreate,
    JobEventOut,
    JobLogsOut,
    JobOut,
)
from app.schemas.marketplace import money_str
from app.services import billing
from app.services import job_fsm as fsm
from app.services.audit import record_audit
from app.services.ledger import InsufficientFunds, lock_customer, to_minor
from app.services.leases import recover_expired_leases, release_job_reservation
from app.services.scheduler import JobValidationError, schedule_job, validate_job
from app.services.spend_limits import SpendLimitExceeded, spend_guard
from app.services.storage import get_store, grant_expiry, make_download_grant

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])

_JOB_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
_ARTIFACT_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found"
)
_PROJECT_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
)


def shape_job(job: Job) -> JobOut:
    return JobOut(
        job_id=job.id,
        customer_id=job.customer_id,
        project_id=job.project_id,
        workload_type=job.workload_type,
        model=job.model,
        input=job.input,
        status=job.status,
        execution_mode=job.execution_mode,
        isolation_tier=getattr(job, "isolation_tier", "container") or "container",
        machine_id=job.machine_id,
        reservation_id=job.reservation_id,
        gpu_count=job.gpu_count,
        estimated_minutes=job.estimated_minutes,
        max_cost=money_str(job.max_cost),
        max_runtime_seconds=job.max_runtime_seconds,
        retry_policy=job.retry_policy or {},
        attempts_made=job.attempts_made,
        priority=job.priority,
        idempotency_key=job.idempotency_key,
        lease_expires_at=ensure_aware(job.lease_expires_at),
        match_explanation=job.match_explanation,
        verification=job.verification,
        output=job.output,
        metrics=job.metrics,
        final_cost=money_str(job.final_cost),
        error_code=job.error_code,
        error_message=job.error_message,
        cancel_requested=job.cancel_requested,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=ensure_aware(job.started_at),
        finished_at=ensure_aware(job.finished_at),
    )


async def _get_own_job(
    db: AsyncSession, customer_id: uuid.UUID, job_id: uuid.UUID, *, lock: bool = False
) -> Job | None:
    stmt = select(Job).where(Job.id == job_id, Job.customer_id == customer_id)
    if lock:
        # Serializes a cancel against the agent's result POST, which reads the
        # same status to decide between releasing and capturing the hold.
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _assert_project_accessible(
    db: AsyncSession, project_id: uuid.UUID | None, user: CurrentUser
) -> None:
    """404 unless the caller owns the project or belongs to its org.

    WHY: ``project_id`` is the scope key for project spend limits and for
    project-scoped reporting. Accepting an unvalidated id let any caller attach
    their jobs to someone else's budget envelope (polluting it) or to an id that
    exists only to dodge a scope they are actually subject to.
    """
    if project_id is None:
        return
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise _PROJECT_NOT_FOUND
    if project.owner_id == user.id:
        return
    if project.org_id is not None:
        member = (
            await db.execute(
                select(OrganizationMember.org_id).where(
                    OrganizationMember.org_id == project.org_id,
                    OrganizationMember.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if member is not None:
            return
    raise _PROJECT_NOT_FOUND


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> JobOut:
    await lock_customer(db, current.id)

    # Idempotency: same (customer, key) returns the SAME job (no duplicate).
    if idempotency_key:
        existing = await db.execute(
            select(Job).where(
                Job.customer_id == current.id,
                Job.idempotency_key == idempotency_key,
            )
        )
        prior = existing.scalar_one_or_none()
        if prior is not None:
            return shape_job(prior)

    await _assert_project_accessible(db, body.project_id, current)

    # A targeted machine must exist BEFORE the insert: jobs.machine_id is a foreign
    # key, so an unknown id used to fail the flush and surface as a bare 500 (which
    # the browser then reports as a CORS error) instead of machine_not_found.
    if body.machine_id is not None:
        known = await db.scalar(
            select(ProviderMachine.id).where(
                ProviderMachine.id == body.machine_id,
                ProviderMachine.status != "deregistered",
            )
        )
        if known is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="machine_not_found")

    retry_policy = body.retry_policy.model_dump() if body.retry_policy else {"max_retries": 0}
    job = Job(
        customer_id=current.id,
        project_id=body.project_id,
        workload_type=body.workload_type,
        model=body.model,
        input=body.input,
        status=fsm.CREATED,
        gpu_count=body.gpu_count,
        estimated_minutes=body.estimated_minutes,
        max_cost=body.max_cost,
        max_runtime_seconds=body.max_runtime_seconds or settings.JOB_MAX_RUNTIME_DEFAULT,
        retry_policy=retry_policy,
        priority=body.priority,
        execution_mode="simulated",
        isolation_tier=body.isolation_tier or "container",
        idempotency_key=idempotency_key,
        machine_id=body.machine_id,
    )
    db.add(job)
    await db.flush()

    # CREATED -> VALIDATING
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
        # Persist the FAILED job (the pipeline edge is honored) before surfacing the error.
        await db.commit()
        raise HTTPException(status_code=exc.http_status, detail=exc.code)

    # VALIDATING -> QUEUED
    await fsm.transition(db, job, fsm.QUEUED, reason="validated", actor="system")
    await record_audit(
        db, action="job.create", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="job", resource_id=str(job.id),
    )

    # Scheduling pass (QUEUED -> MATCHED -> RESERVED). No eligible provider leaves
    # the job QUEUED (retried later by the sweeper / run_scheduler). The scheduler
    # HOLDs the authorized amount before reserving capacity; an insufficient balance
    # surfaces as 402 insufficient_credits and the job -> FAILED (no capacity held).
    # Authorized ceiling for the budget check. ``max_cost`` is the customer's own
    # hard ceiling when set; otherwise 0 still blocks a submit by an already-capped
    # customer, and the post-spend alert step inside the guard sees the real figure.
    authorized_minor = to_minor(job.max_cost) if job.max_cost is not None else 0
    try:
        async with spend_guard(db, current.id, job.project_id, authorized_minor):
            await schedule_job(db, job)
            # MUST commit inside the guard: the per-customer gate is released at
            # block exit, so the next waiter has to see this spend in the ledger
            # or two concurrent submits could both pass the same cap.
            await db.commit()
    except SpendLimitExceeded as exc:
        # Raised by the guard BEFORE it yields, so nothing was written inside the
        # guarded block and the session is clean — no rollback (which would also
        # discard the job row and its FSM history we are about to persist).
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
            ip=client_ip(request), resource_type="job", resource_id=str(job.id),
            reason="insufficient_credits",
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="insufficient_credits"
        )
    await db.flush()
    return shape_job(job)


@router.get("", response_model=list[JobOut])
async def list_jobs(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[JobOut]:
    result = await db.execute(
        select(Job).where(Job.customer_id == current.id).order_by(Job.created_at.desc())
    )
    return [shape_job(j) for j in result.scalars().all()]


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobOut:
    job = await _get_own_job(db, current.id, job_id)
    if job is None:
        raise _JOB_NOT_FOUND
    # Lazy lease recovery on read (a disconnected provider never strands a job).
    if job.status == fsm.RUNNING:
        now = utcnow()
        exp = ensure_aware(job.lease_expires_at)
        if exp is not None and exp <= now:
            await recover_expired_leases(db, now)
            await db.refresh(job)
    return shape_job(job)


@router.get("/{job_id}/events", response_model=list[JobEventOut])
async def get_job_events(
    job_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[JobEventOut]:
    job = await _get_own_job(db, current.id, job_id)
    if job is None:
        raise _JOB_NOT_FOUND
    result = await db.execute(
        select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.seq)
    )
    return [
        JobEventOut(
            seq=e.seq,
            from_state=e.from_state,
            to_state=e.to_state,
            reason=e.reason,
            actor=e.actor,
            created_at=e.created_at,
        )
        for e in result.scalars().all()
    ]


@router.get("/{job_id}/logs", response_model=JobLogsOut)
async def get_job_logs(
    job_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobLogsOut:
    job = await _get_own_job(db, current.id, job_id)
    if job is None:
        raise _JOB_NOT_FOUND
    result = await db.execute(
        select(JobLogMetadata).where(JobLogMetadata.job_id == job.id).order_by(JobLogMetadata.seq)
    )
    store = get_store()
    lines: list[str] = []
    for meta in result.scalars().all():
        try:
            chunk = store.get(meta.object_key).decode("utf-8")
        except (FileNotFoundError, OSError):
            continue
        lines.extend(line for line in chunk.split("\n") if line != "")
    return JobLogsOut(job_id=job.id, lines=lines)


@router.get("/{job_id}/artifacts", response_model=list[ArtifactOut])
async def get_job_artifacts(
    job_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ArtifactOut]:
    job = await _get_own_job(db, current.id, job_id)
    if job is None:
        raise _JOB_NOT_FOUND
    result = await db.execute(
        select(Artifact).where(Artifact.job_id == job.id).order_by(Artifact.created_at)
    )
    return [
        ArtifactOut(
            artifact_id=a.id,
            kind=a.kind,
            size=a.size_bytes,
            sha256=a.sha256,
            content_type=a.content_type,
            created_at=a.created_at,
        )
        for a in result.scalars().all()
    ]


@router.get("/{job_id}/artifacts/{artifact_id}/download", response_model=ArtifactDownloadOut)
async def get_artifact_download_url(
    job_id: uuid.UUID,
    artifact_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ArtifactDownloadOut:
    job = await _get_own_job(db, current.id, job_id)
    if job is None:
        raise _JOB_NOT_FOUND
    result = await db.execute(
        select(Artifact).where(Artifact.id == artifact_id, Artifact.job_id == job.id)
    )
    artifact = result.scalar_one_or_none()
    if artifact is None:
        raise _ARTIFACT_NOT_FOUND
    expires_at = grant_expiry()
    token = make_download_grant(str(artifact.id), expires_at)
    return ArtifactDownloadOut(
        artifact_id=artifact.id,
        url=f"/v1/artifacts/download/{token}",
        expires_at=expires_at,
    )


@router.post("/{job_id}/cancel", response_model=JobCancelOut)
async def cancel_job(
    job_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobCancelOut:
    job = await _get_own_job(db, current.id, job_id, lock=True)
    if job is None:
        raise _JOB_NOT_FOUND

    job.cancel_requested = True
    # Honored at the next safe point — at MVP (simulated/synchronous) that is now.
    if job.status in fsm.CANCELLABLE_STATES:
        await release_job_reservation(db, job, utcnow())
        await fsm.transition(db, job, fsm.CANCELLED, reason="customer_cancel", actor="customer")
        # A cancelled job is never charged — return the full hold to the customer.
        await billing.release_hold(db, job)
        await record_audit(
            db, action="job.cancel", outcome="success", actor_user_id=current.id,
            ip=client_ip(request), resource_type="job", resource_id=str(job.id),
        )
    await db.flush()
    return JobCancelOut(
        job_id=job.id, status=job.status, cancel_requested=job.cancel_requested
    )
