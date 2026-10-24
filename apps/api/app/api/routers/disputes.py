"""Customer dispute endpoints (JWT, owner-scoped) + admin adjudication.

Phase B5 additions:

* Opening a dispute now auto-attaches a :class:`DisputeEvidence` snapshot of the
  job (input digest, output digest, artifact hashes, verification/redundancy
  results). Capturing at open-time matters: artifacts can be garbage-collected
  and a provider could re-upload, so the record has to be frozen the moment the
  customer complains.
* Customers read only their own dispute and a **redacted** evidence view; the
  counterparty's private fields never cross the boundary (404 to non-owners —
  never 403, which would confirm the id exists).
* Administrators get the unredacted evidence view and an audited adjudication
  endpoint whose refund goes through the existing billing refund path.
"""

from __future__ import annotations

import decimal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps import client_ip, get_current_user, require_roles
from app.models.billing import Dispute
from app.models.jobs import Job
from app.schemas.billing import parse_money
from app.schemas.auth import CurrentUser
from app.schemas.billing import DisputeCreate, DisputeOut
from app.services import disputes as dispute_svc
from app.services import job_fsm as fsm
from app.services import ledger
from app.services.audit import record_audit

router = APIRouter(prefix="/v1", tags=["disputes"])

# Admin adjudication lives on its own router so it can carry a blanket RBAC
# dependency. It intentionally does NOT reuse the legacy
# ``/v1/admin/disputes/{id}/resolve`` path in app/api/routers/admin.py: shadowing
# a live, wired route by include order is a trap for the next reader.
admin_router = APIRouter(
    prefix="/v1/admin",
    tags=["disputes-admin"],
    dependencies=[Depends(require_roles("administrator"))],
)

_DISPUTE_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Dispute not found"
)
_JOB_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")


class DisputeAdjudicate(BaseModel):
    """Admin adjudication body. ``refund_amount`` is major units as a string or
    number and is converted to integer minor units — never float arithmetic."""

    resolution: str = Field(min_length=1, max_length=1024)
    refund_amount: decimal.Decimal | None = None

    @field_validator("refund_amount", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> decimal.Decimal | None:
        return None if v is None else parse_money(v)


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


async def _owned_dispute(
    db: AsyncSession, dispute_id: uuid.UUID, customer_id: uuid.UUID
) -> Dispute:
    """Load a dispute scoped to its owner, 404 (not 403) to everyone else."""
    d = (
        await db.execute(
            select(Dispute).where(
                Dispute.id == dispute_id, Dispute.customer_id == customer_id
            )
        )
    ).scalar_one_or_none()
    if d is None:
        raise _DISPUTE_NOT_FOUND
    return d


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
            # Row lock: two simultaneous submits for one job must not both pass the
            # "already open?" check below.
            select(Job).where(Job.id == job_id, Job.customer_id == current.id).with_for_update()
        )
    ).scalar_one_or_none()
    if job is None:
        raise _JOB_NOT_FOUND
    # A dispute is only meaningful on a settled (completed) job.
    if job.status not in (fsm.COMPLETED, fsm.DISPUTED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="job_not_settled"
        )

    # One active dispute per job. Nothing used to enforce this, so every extra POST
    # created another dispute (and another evidence bundle) for an admin to triage.
    already_open = await db.scalar(
        select(Dispute.id).where(Dispute.job_id == job.id, Dispute.status == "open").limit(1)
    )
    if already_open is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="dispute_exists")

    dispute = Dispute(job_id=job.id, customer_id=current.id, reason=body.reason, status="open")
    db.add(dispute)
    # Flush so the evidence rows have a real dispute_id to hang off.
    await db.flush()
    await dispute_svc.collect_evidence(
        db, dispute=dispute, job=job, submitted_by_user_id=current.id
    )
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
    return shape_dispute(await _owned_dispute(db, dispute_id, current.id))


@router.get("/disputes/{dispute_id}/evidence")
async def get_dispute_evidence(
    dispute_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Owner-only, redacted evidence view.

    Redaction is applied server-side (``viewer="customer"``) so provider-private
    pointers such as machine ids and object keys never reach the customer.
    """
    dispute = await _owned_dispute(db, dispute_id, current.id)
    return {
        "dispute_id": str(dispute.id),
        "job_id": str(dispute.job_id),
        "status": dispute.status,
        "evidence": await dispute_svc.list_evidence(db, dispute.id, viewer="customer"),
    }


# ---------------------------------------------------------------------------
# Admin adjudication
# ---------------------------------------------------------------------------
@admin_router.get("/disputes")
async def admin_list_disputes(
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    stmt = select(Dispute).order_by(Dispute.created_at.desc())
    if status_filter:
        stmt = stmt.where(Dispute.status == status_filter)
    rows = (await db.execute(stmt)).scalars().all()
    return [shape_dispute(d).model_dump(mode="json") for d in rows]


@admin_router.get("/disputes/{dispute_id}/evidence")
async def admin_dispute_evidence(
    dispute_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Unredacted evidence view — an adjudicator must see both sides."""
    dispute = await db.get(Dispute, dispute_id)
    if dispute is None:
        raise _DISPUTE_NOT_FOUND
    return {
        "dispute_id": str(dispute.id),
        "job_id": str(dispute.job_id),
        "customer_id": str(dispute.customer_id),
        "status": dispute.status,
        "reason": dispute.reason,
        "evidence": await dispute_svc.list_evidence(db, dispute.id, viewer="admin"),
    }


@admin_router.post("/disputes/{dispute_id}/adjudicate")
async def admin_adjudicate_dispute(
    dispute_id: uuid.UUID,
    body: DisputeAdjudicate,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Resolve a dispute, optionally refunding through the billing choke point."""
    dispute = await db.get(Dispute, dispute_id)
    if dispute is None:
        raise _DISPUTE_NOT_FOUND
    refund_minor = (
        ledger.to_minor(body.refund_amount) if body.refund_amount is not None else None
    )
    try:
        out = await dispute_svc.resolve_dispute(
            db,
            dispute=dispute,
            resolution=body.resolution,
            refund_amount_minor=refund_minor,
            actor_user_id=current.id,
            ip=client_ip(request),
        )
    except dispute_svc.DisputeResolutionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ValueError as exc:
        # billing_svc.refund rejects over-cap / unfunded refunds.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    await db.commit()
    return out
