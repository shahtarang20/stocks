"""Customer dispute endpoints (JWT, owner-scoped)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps import client_ip, get_current_user
from app.models.billing import Dispute
from app.models.jobs import Job
from app.schemas.auth import CurrentUser
from app.schemas.billing import DisputeCreate, DisputeOut
from app.services import job_fsm as fsm
from app.services import ledger
from app.services.audit import record_audit

router = APIRouter(prefix="/v1", tags=["disputes"])

_DISPUTE_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Dispute not found"
)
_JOB_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")


def shape_dispute(d: Dispute) -> DisputeOut:
    return DisputeOut(
        dispute_id=d.id,
        job_id=d.job_id,
        reason=d.reason,
        status=d.status,
        resolution=d.resolution,
        refund_amount=(
            ledger.minor_to_str(d.refund_amount_minor)
            if d.refund_amount_minor is not None
            else None
        ),
        created_at=d.created_at,
        resolved_at=d.resolved_at,
    )


@router.post("/jobs/{job_id}/dispute", response_model=DisputeOut, status_code=status.HTTP_201_CREATED)
async def open_dispute(
    job_id: uuid.UUID,
    body: DisputeCreate,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DisputeOut:
    job = (
        await db.execute(
            select(Job).where(Job.id == job_id, Job.customer_id == current.id)
        )
    ).scalar_one_or_none()
    if job is None:
        raise _JOB_NOT_FOUND
    # A dispute is only meaningful on a settled (completed) job.
    if job.status not in (fsm.COMPLETED, fsm.DISPUTED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="job_not_settled"
        )

    dispute = Dispute(job_id=job.id, customer_id=current.id, reason=body.reason, status="open")
    db.add(dispute)
    if job.status == fsm.COMPLETED:
        await fsm.transition(db, job, fsm.DISPUTED, reason="customer_dispute", actor="customer")
    await record_audit(
        db, action="dispute.open", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="dispute", resource_id=str(dispute.id),
    )
    await db.commit()
    await db.refresh(dispute)
    return shape_dispute(dispute)


@router.get("/disputes", response_model=list[DisputeOut])
async def list_disputes(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DisputeOut]:
    rows = (
        await db.execute(
            select(Dispute)
            .where(Dispute.customer_id == current.id)
            .order_by(Dispute.created_at.desc())
        )
    ).scalars().all()
    return [shape_dispute(d) for d in rows]


@router.get("/disputes/{dispute_id}", response_model=DisputeOut)
async def get_dispute(
    dispute_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DisputeOut:
    d = (
        await db.execute(
            select(Dispute).where(
                Dispute.id == dispute_id, Dispute.customer_id == current.id
            )
        )
    ).scalar_one_or_none()
    if d is None:
        raise _DISPUTE_NOT_FOUND
    return shape_dispute(d)
