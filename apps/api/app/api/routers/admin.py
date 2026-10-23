"""Admin-only endpoints (RBAC) + Phase 5 money operations.

Every admin money move requires the ``administrator`` role, is a balanced ledger
transaction, and writes an audit log with actor + reason (least-privilege, no
silent balance mutation).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import get_db
from app.deps import client_ip, require_roles
from app.models.billing import (
    BillingAccount,
    Dispute,
    Invoice,
    Payment,
    ProviderPayout,
    Refund,
)
from app.models.enums import UserStatus
from app.models.jobs import Job
from app.models.provider import ProviderMachine
from app.models.system import AuditLog
from app.models.user import User
from app.schemas.admin import (
    AdminAuditLogOut,
    AdminJobOut,
    AdminPlatformStatsOut,
    AdminUserActionResponse,
    AdminUserOut,
)
from app.schemas.auth import CurrentUser
from app.schemas.marketplace import money_str
from app.schemas.provider import MachineOut
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
from app.services import reputation as reputation_svc
from app.services.provider import _machine_query, shape_machine
from app.services.audit import record_audit
from app.services.hooks import safe_hook

logger = logging.getLogger(__name__)

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

    # If payment was made via Stripe and Stripe is active, dispatch Stripe refund
    if settings.PAYMENT_PROVIDER == "stripe":
        payment_query = await db.execute(
            select(Payment)
            .where(
                Payment.customer_id == job.customer_id,
                Payment.provider_configuration == "stripe",
                Payment.stripe_payment_intent_id.is_not(None),
            )
            .order_by(Payment.created_at.desc())
        )
        payment = payment_query.scalars().first()
        if payment and payment.stripe_payment_intent_id:
            try:
                from app.services.stripe_payment import get_payment_provider
                provider = get_payment_provider()
                res = await provider.refund_payment(
                    db,
                    payment_intent_id=payment.stripe_payment_intent_id,
                    amount_minor=amount_minor,
                    reason=body.reason,
                )
                refund.stripe_refund_id = res.get("refund_id")
            except Exception as exc:
                logger.warning("Stripe refund dispatch error: %s", exc)

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
        # A refunded dispute is one the provider lost. Reputation is reporting,
        # not bookkeeping: a failure here must never undo the refund above.
        if job.machine_id is not None:
            async with safe_hook(db, "reputation.dispute_lost"):
                machine = await db.get(ProviderMachine, job.machine_id)
                if machine is not None:
                    await reputation_svc.recompute_reputation(
                        db, machine.provider_id, event_kind="dispute_lost",
                        reason=body.resolution, job_id=job.id, machine_id=machine.id,
                    )

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


# ---------------------------------------------------------------------------
# Phase 7 — User management & suspension
# ---------------------------------------------------------------------------
@money_router.get("/users", response_model=list[AdminUserOut])
async def list_users(
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> list[AdminUserOut]:
    """List all platform users with role assignments and statuses."""
    result = await db.execute(
        select(User).options(selectinload(User.roles)).order_by(User.created_at.desc())
    )
    users = result.scalars().all()
    return [
        AdminUserOut(
            user_id=u.id,
            email=u.email,
            status=u.status.value if hasattr(u.status, "value") else str(u.status),
            roles=[r.role.value if hasattr(r.role, "value") else str(r.role) for r in u.roles],
            email_verified=u.email_verified,
            created_at=u.created_at,
        )
        for u in users
    ]


@money_router.post("/users/{user_id}/suspend", response_model=AdminUserActionResponse)
async def suspend_user(
    user_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> AdminUserActionResponse:
    """Suspend a user account and write an audited record."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found")
    if user.id == current.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cannot_suspend_self")
    user.status = UserStatus.suspended
    await record_audit(
        db, action="admin.user.suspend", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="user", resource_id=str(user.id),
    )
    await db.commit()
    return AdminUserActionResponse(
        user_id=user.id,
        email=user.email,
        status="suspended",
        message="User has been suspended successfully",
    )


@money_router.post("/users/{user_id}/reinstate", response_model=AdminUserActionResponse)
async def reinstate_user(
    user_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> AdminUserActionResponse:
    """Reinstate a suspended user account."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found")
    user.status = UserStatus.active
    await record_audit(
        db, action="admin.user.reinstate", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="user", resource_id=str(user.id),
    )
    await db.commit()
    return AdminUserActionResponse(
        user_id=user.id,
        email=user.email,
        status="active",
        message="User has been reinstated successfully",
    )


# ---------------------------------------------------------------------------
# Phase 7 — Audit logs
# ---------------------------------------------------------------------------
@money_router.get("/audit-logs", response_model=list[AdminAuditLogOut])
async def list_audit_logs(
    limit: int = 50,
    offset: int = 0,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> list[AdminAuditLogOut]:
    """Read immutable platform audit logs."""
    result = await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(limit, 100)).offset(offset)
    )
    entries = result.scalars().all()
    return [
        AdminAuditLogOut(
            id=e.id,
            actor_user_id=e.actor_user_id,
            action=e.action,
            resource_type=e.resource_type,
            resource_id=e.resource_id,
            ip=e.ip,
            reason=e.reason,
            outcome=e.outcome,
            created_at=e.created_at,
        )
        for e in entries
    ]


# ---------------------------------------------------------------------------
# Phase 7 — All Platform Jobs
# ---------------------------------------------------------------------------
@money_router.get("/jobs", response_model=list[AdminJobOut])
async def list_admin_jobs(
    limit: int = 50,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> list[AdminJobOut]:
    """List all jobs across all users in the network."""
    result = await db.execute(
        select(Job).order_by(Job.created_at.desc()).limit(min(limit, 100))
    )
    jobs = result.scalars().all()
    return [
        AdminJobOut(
            job_id=j.id,
            customer_id=j.customer_id,
            workload_type=j.workload_type,
            model=j.model,
            status=j.status,
            execution_mode=j.execution_mode,
            created_at=j.created_at,
            # Job.final_cost is a Numeric(14,2) Decimal, NOT minor units, so it
            # is formatted the same way /v1/jobs serializes it. Reading a
            # non-existent `final_cost_minor` here made this endpoint a hard 500
            # for any non-empty job list.
            final_cost=money_str(j.final_cost),
            error_code=j.error_code,
        )
        for j in jobs
    ]


@money_router.get("/machines", response_model=list[MachineOut])
async def list_admin_machines(
    limit: int = 200,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> list[MachineOut]:
    """List every provider machine on the network (the telemetry console's picker).

    /provider/machines is scoped to the caller's own provider profile, so an
    administrator who is not also a provider got a 403 there and could never
    select a machine to inspect.
    """
    result = await db.execute(
        _machine_query()
        .where(ProviderMachine.status != "deregistered")
        .order_by(ProviderMachine.created_at.desc())
        .limit(min(limit, 500))
    )
    return [shape_machine(m) for m in result.scalars().all()]


# ---------------------------------------------------------------------------
# Phase 7 — Platform Statistics
# ---------------------------------------------------------------------------
@money_router.get("/stats", response_model=AdminPlatformStatsOut)
async def platform_stats(
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> AdminPlatformStatsOut:
    """Return live network KPIs."""
    total_users = await db.scalar(select(func.count(User.id))) or 0
    active_providers = await db.scalar(
        select(func.count(ProviderMachine.id)).where(
            ProviderMachine.status != "deregistered",
            ProviderMachine.availability == "online",
        )
    ) or 0
    total_jobs = await db.scalar(select(func.count(Job.id))) or 0
    completed_jobs = await db.scalar(
        select(func.count(Job.id)).where(Job.status == "completed")
    ) or 0
    success_rate = round((completed_jobs / total_jobs * 100.0), 1) if total_jobs > 0 else 100.0
    total_debits, _ = await ledger.ledger_totals(db)
    return AdminPlatformStatsOut(
        total_users=total_users,
        active_providers=active_providers,
        total_jobs=total_jobs,
        completed_jobs=completed_jobs,
        success_rate=success_rate,
        total_volume=ledger.minor_to_str(total_debits),
    )

