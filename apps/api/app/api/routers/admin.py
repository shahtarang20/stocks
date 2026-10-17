"""Admin-only endpoints (RBAC) + Phase 5 money operations.

Every admin money move requires the ``administrator`` role, is a balanced ledger
transaction, and writes an audit log with actor + reason (least-privilege, no
silent balance mutation).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.session import get_db
from app.deps import client_ip, require_roles
from app.models.billing import (
    BillingAccount,
    Dispute,
    Invoice,
    ProviderPayout,
    Refund,
)
from app.models.jobs import Job
from app.schemas.auth import CurrentUser
from app.schemas.billing import (
    DisputeResolve,
    PayoutOut,
    ReconciliationAccountOut,
    ReconciliationOut,
    RefundCreate,
    RefundOut,
)
from app.services import billing as billing_svc
from app.services import ledger
from app.services.audit import record_audit

router = APIRouter(prefix="/admin", tags=["admin"])

# Phase 5 admin money routes under the versioned /v1/admin prefix per contract.
money_router = APIRouter(prefix="/v1/admin", tags=["admin-billing"])


@router.get("/ping")
async def admin_ping(
    current: CurrentUser = Depends(require_roles("administrator")),
) -> dict:
    """Trivial admin-only route guarded by require_roles('administrator')."""
    return {"status": "ok", "actor": str(current.id), "roles": current.roles}


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------
@money_router.post("/refunds", response_model=RefundOut, status_code=status.HTTP_201_CREATED)
async def create_refund(
    body: RefundCreate,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> RefundOut:
    if body.job_id is None and body.invoice_id is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="job_or_invoice_required")

    invoice: Invoice | None = None
    if body.invoice_id is not None:
        invoice = (
            await db.execute(select(Invoice).where(Invoice.id == body.invoice_id))
        ).scalar_one_or_none()
        if invoice is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invoice_not_found")
        job_id = invoice.job_id
    else:
        job_id = body.job_id

    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job_not_found")
    if invoice is None:
        invoice = (
            await db.execute(select(Invoice).where(Invoice.job_id == job.id))
        ).scalar_one_or_none()

    amount_minor = ledger.to_minor(body.amount)
    refund = Refund(
        job_id=job.id,
        invoice_id=invoice.id if invoice else None,
        customer_id=job.customer_id,
        amount_minor=amount_minor,
        reason=body.reason,
        actor_user_id=current.id,
    )
    db.add(refund)
    await db.flush()
    try:
        txn = await billing_svc.refund(
            db,
            job=job,
            amount_minor=amount_minor,
            actor_user_id=current.id,
            reason=body.reason,
            idempotency_key=f"refund:{refund.id}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    refund.transaction_id = txn.id
    if invoice is not None:
        invoice.status = "refunded"
    await record_audit(
        db, action="admin.refund", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="refund", resource_id=str(refund.id),
        reason=body.reason,
    )
    await db.commit()
    await db.refresh(refund)
    return RefundOut(
        refund_id=refund.id,
        job_id=refund.job_id,
        amount=ledger.minor_to_str(refund.amount_minor),
        reason=refund.reason,
        created_at=refund.created_at,
    )


# ---------------------------------------------------------------------------
# Payout authorization
# ---------------------------------------------------------------------------
@money_router.post("/payouts/{payout_id}/authorize", response_model=PayoutOut)
async def authorize_payout(
    payout_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> PayoutOut:
    payout = await db.get(ProviderPayout, payout_id)
    if payout is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="payout_not_found")
    if payout.status != "requested":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payout_not_pending")

    payout_pending = await ledger.get_platform_account(db, "payout_pending")
    external = await ledger.get_platform_account(db, "external")
    txn = await ledger.post_transaction(
        db,
        kind="payout_settle",
        legs=[
            (payout_pending, "debit", payout.amount_minor),
            (external, "credit", payout.amount_minor),
        ],
        idempotency_key=f"payout_settle:{payout.id}",
        reference_type="payout",
        reference_id=str(payout.id),
        actor_user_id=current.id,
    )
    payout.status = "settled"
    payout.settle_txn_id = txn.id
    payout.settled_at = utcnow()
    await record_audit(
        db, action="admin.payout.authorize", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="payout", resource_id=str(payout.id),
    )
    await db.commit()
    await db.refresh(payout)
    return PayoutOut(
        payout_id=payout.id,
        amount=ledger.minor_to_str(payout.amount_minor),
        currency=payout.currency,
        status=payout.status,
        created_at=payout.created_at,
        settled_at=payout.settled_at,
    )


# ---------------------------------------------------------------------------
# Dispute resolution
# ---------------------------------------------------------------------------
@money_router.post("/disputes/{dispute_id}/resolve")
async def resolve_dispute(
    dispute_id: uuid.UUID,
    body: DisputeResolve,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    dispute = await db.get(Dispute, dispute_id)
    if dispute is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="dispute_not_found")
    if dispute.status != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="dispute_not_open")

    dispute.resolution = body.resolution
    dispute.status = "resolved"
    dispute.resolved_at = utcnow()
    dispute.actor_user_id = current.id

    if body.refund_amount is not None:
        job = await db.get(Job, dispute.job_id)
        amount_minor = ledger.to_minor(body.refund_amount)
        refund = Refund(
            job_id=job.id,
            customer_id=job.customer_id,
            amount_minor=amount_minor,
            reason=f"dispute_resolution:{body.resolution}"[:512],
            actor_user_id=current.id,
        )
        db.add(refund)
        await db.flush()
        try:
            txn = await billing_svc.refund(
                db, job=job, amount_minor=amount_minor, actor_user_id=current.id,
                reason=body.resolution, idempotency_key=f"refund:{refund.id}",
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        refund.transaction_id = txn.id
        dispute.refund_amount_minor = amount_minor

    await record_audit(
        db, action="admin.dispute.resolve", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="dispute", resource_id=str(dispute.id),
        reason=body.resolution,
    )
    await db.commit()
    return {
        "dispute_id": str(dispute.id),
        "status": dispute.status,
        "resolution": dispute.resolution,
        "refund_amount": (
            ledger.minor_to_str(dispute.refund_amount_minor)
            if dispute.refund_amount_minor is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Reconciliation (INVARIANT 2 surfaced)
# ---------------------------------------------------------------------------
@money_router.get("/reconciliation", response_model=ReconciliationOut)
async def reconciliation(
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> ReconciliationOut:
    total_debits, total_credits = await ledger.ledger_totals(db)
    imbalance = total_debits - total_credits
    accounts = (
        await db.execute(select(BillingAccount).order_by(BillingAccount.type))
    ).scalars().all()
    return ReconciliationOut(
        balanced=(imbalance == 0),
        total_debits=ledger.minor_to_str(total_debits),
        total_credits=ledger.minor_to_str(total_credits),
        imbalance=ledger.minor_to_str(imbalance),
        accounts=[
            ReconciliationAccountOut(
                account_id=a.id,
                owner_type=a.owner_type,
                type=a.type,
                balance=ledger.minor_to_str(a.balance_minor),
            )
            for a in accounts
        ],
    )
