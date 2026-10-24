"""Verification endpoints (Phase B2).

Two audiences, two authorization rules:

* **Administrators** manage known-answer challenges and read the full
  verification-evidence stream (this is audit data about providers, so it is
  never public).
* **Customers** may read the verification evidence for THEIR OWN job only. A
  job belonging to somebody else returns 404 rather than 403 — a 403 would
  confirm the job id exists and leak the platform's job-id space.

Challenge rows carry the answers to the anti-cheating audit, so nothing here is
reachable by a provider or an agent.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps import client_ip, get_current_user, require_roles
from app.models.jobs import Job
from app.models.verification import (
    RedundancyCheck,
    VerificationChallenge,
    VerificationResult,
)
from app.schemas.auth import CurrentUser
from app.schemas.verification import (
    ChallengeCreate,
    ChallengeOut,
    RedundancyCheckOut,
    VerificationResultOut,
)
from app.services import challenge as challenge_service
from app.services.audit import record_audit

router = APIRouter(prefix="/v1", tags=["verification"])

_JOB_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
)

_ADMIN = Depends(require_roles("administrator"))


def shape_challenge(row: VerificationChallenge) -> ChallengeOut:
    return ChallengeOut(
        challenge_id=row.id,
        workload_type=row.workload_type,
        model=row.model,
        input=row.input or {},
        expected_output_hash=row.expected_output_hash,
        config=row.config or {},
        label=row.label,
        active=row.active,
        created_at=row.created_at,
    )


def shape_result(row: VerificationResult) -> VerificationResultOut:
    return VerificationResultOut(
        result_id=row.id,
        job_id=row.job_id,
        challenge_id=row.challenge_id,
        kind=row.kind,
        passed=row.passed,
        expected_hash=row.expected_hash,
        actual_hash=row.actual_hash,
        detail=row.detail,
        created_at=row.created_at,
    )


def shape_check(row: RedundancyCheck) -> RedundancyCheckOut:
    return RedundancyCheckOut(
        check_id=row.id,
        primary_job_id=row.primary_job_id,
        shadow_job_id=row.shadow_job_id,
        status=row.status,
        matched=row.matched,
        similarity=str(row.similarity) if row.similarity is not None else None,
        detail=row.detail,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
    )


# ---------------------------------------------------------------------------
# Admin: challenges
# ---------------------------------------------------------------------------
@router.post(
    "/admin/verification/challenges",
    response_model=ChallengeOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_challenge(
    body: ChallengeCreate,
    request: Request,
    current: CurrentUser = _ADMIN,
    db: AsyncSession = Depends(get_db),
) -> ChallengeOut:
    """Seed a known-answer challenge (administrator only)."""
    row = await challenge_service.create_challenge(
        db,
        workload_type=body.workload_type,
        model=body.model,
        input=body.input,
        expected_output=body.expected_output,
        expected_output_hash=body.expected_output_hash,
        config=body.config,
        label=body.label,
        active=body.active,
    )
    await record_audit(
        db,
        action="verification.challenge.create",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="verification_challenge",
        resource_id=str(row.id),
    )
    await db.commit()
    return shape_challenge(row)


@router.get("/admin/verification/challenges", response_model=list[ChallengeOut])
async def list_challenges(
    workload_type: str | None = None,
    model: str | None = None,
    active: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=2_000_000_000),
    current: CurrentUser = _ADMIN,
    db: AsyncSession = Depends(get_db),
) -> list[ChallengeOut]:
    query = select(VerificationChallenge)
    if workload_type is not None:
        query = query.where(VerificationChallenge.workload_type == workload_type)
    if model is not None:
        query = query.where(VerificationChallenge.model == model)
    if active is not None:
        query = query.where(VerificationChallenge.active.is_(active))
    rows = (
        await db.execute(
            query.order_by(VerificationChallenge.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return [shape_challenge(r) for r in rows]


# ---------------------------------------------------------------------------
# Admin: evidence
# ---------------------------------------------------------------------------
@router.get("/admin/verification/results", response_model=list[VerificationResultOut])
async def list_results(
    job_id: uuid.UUID | None = None,
    kind: str | None = None,
    passed: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=2_000_000_000),
    current: CurrentUser = _ADMIN,
    db: AsyncSession = Depends(get_db),
) -> list[VerificationResultOut]:
    """Full verification-evidence stream, newest first (administrator only)."""
    query = select(VerificationResult)
    if job_id is not None:
        query = query.where(VerificationResult.job_id == job_id)
    if kind is not None:
        query = query.where(VerificationResult.kind == kind)
    if passed is not None:
        query = query.where(VerificationResult.passed.is_(passed))
    rows = (
        await db.execute(
            query.order_by(VerificationResult.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return [shape_result(r) for r in rows]


@router.get(
    "/admin/verification/redundancy-checks", response_model=list[RedundancyCheckOut]
)
async def list_redundancy_checks(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=2_000_000_000),
    current: CurrentUser = _ADMIN,
    db: AsyncSession = Depends(get_db),
) -> list[RedundancyCheckOut]:
    query = select(RedundancyCheck)
    if status_filter is not None:
        query = query.where(RedundancyCheck.status == status_filter)
    rows = (
        await db.execute(
            query.order_by(RedundancyCheck.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return [shape_check(r) for r in rows]


# ---------------------------------------------------------------------------
# Customer: own job only
# ---------------------------------------------------------------------------
@router.get(
    "/jobs/{job_id}/verification-results", response_model=list[VerificationResultOut]
)
async def list_job_results(
    job_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[VerificationResultOut]:
    """Verification evidence for the caller's OWN job (404 to anyone else)."""
    job = (
        await db.execute(
            select(Job).where(Job.id == job_id, Job.customer_id == current.id)
        )
    ).scalar_one_or_none()
    if job is None:
        raise _JOB_NOT_FOUND
    rows = (
        await db.execute(
            select(VerificationResult)
            .where(VerificationResult.job_id == job.id)
            .order_by(VerificationResult.created_at.desc())
        )
    ).scalars().all()
    return [shape_result(r) for r in rows]
