"""Customer billing endpoints (JWT, owner-scoped).

GET  /v1/billing/account           — available / reserved / balance
POST /v1/billing/credits/purchase  — simulated top-up (credit_purchase)
GET  /v1/billing/ledger            — the customer's own ledger entries (paginated)
GET  /v1/usage                     — spend summary + usage records
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.deps import client_ip, get_current_user, require_verified_user
from app.models.billing import (
    BillingAccount,
    LedgerEntry,
    LedgerTransaction,
    Payment,
    UsageRecord,
)
from app.schemas.auth import CurrentUser
from app.schemas.billing import (
    AccountOut,
    AmountIn,
    CheckoutSessionCreate,
    CheckoutSessionOut,
    LedgerEntryOut,
    LedgerPageOut,
    StripeWebhookOut,
    UsageRecordOut,
    UsageSummaryOut,
)
from app.services import billing as billing_svc
from app.services import ledger
from app.services.audit import record_audit
from app.services.stripe_payment import (
    PaymentProviderError,
    StripePaymentProvider,
    StripeSignatureError,
    get_payment_provider,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["billing"])


def _shape_entry(entry: LedgerEntry, txn: LedgerTransaction) -> LedgerEntryOut:
    return LedgerEntryOut(
        entry_id=entry.id,
        transaction_id=entry.transaction_id,
        kind=txn.kind,
        direction=entry.direction,
        amount=ledger.minor_to_str(entry.amount_minor),
        currency=entry.currency,
        reference_type=txn.reference_type,
        reference_id=txn.reference_id,
        created_at=entry.created_at,
    )


async def _account_out(db: AsyncSession, user_id: uuid.UUID) -> AccountOut:
    account = await ledger.get_or_create_customer_account(db, user_id)
    available = account.balance_minor
    reserved = await billing_svc.customer_reserved_minor(db, user_id)
    return AccountOut(
        currency=account.currency,
        available=ledger.minor_to_str(available),
        reserved=ledger.minor_to_str(reserved),
        balance=ledger.minor_to_str(available + reserved),
    )


@router.get("/billing/account", response_model=AccountOut)
async def get_account(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AccountOut:
    out = await _account_out(db, current.id)
    await db.commit()
    return out


@router.post("/billing/credits/purchase", response_model=AccountOut)
async def purchase_credits(
    body: AmountIn,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> AccountOut:
    account = await ledger.get_or_create_customer_account(db, current.id)
    external = await ledger.get_platform_account(db, "external")
    amount_minor = ledger.to_minor(body.amount)
    txn = await ledger.post_transaction(
        db,
        kind="credit_purchase",
        legs=[(external, "debit", amount_minor), (account, "credit", amount_minor)],
        idempotency_key=f"purchase:{uuid.uuid4()}",
        reference_type="credit_purchase",
        reference_id=str(current.id),
        actor_user_id=current.id,
    )
    db.add(
        Payment(
            customer_id=current.id,
            amount_minor=amount_minor,
            status="succeeded",
            provider_configuration=settings.PAYMENT_PROVIDER_CONFIGURATION,
            transaction_id=txn.id,
        )
    )
    await record_audit(
        db, action="billing.credits.purchase", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="payment", resource_id=str(txn.id),
        reason=ledger.minor_to_str(amount_minor),
    )
    out = await _account_out(db, current.id)
    await db.commit()
    return out


@router.post("/billing/checkout/session", response_model=CheckoutSessionOut)
async def create_checkout_session(
    body: CheckoutSessionCreate,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CheckoutSessionOut:
    """Create a Stripe Checkout Session for purchasing ComputeCoin credits."""
    provider = get_payment_provider()
    try:
        session_data = await provider.create_checkout_session(
            customer_id=current.id,
            amount=body.amount,
            success_url=body.success_url,
            cancel_url=body.cancel_url,
        )
    except PaymentProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    await record_audit(
        db,
        action="billing.checkout.session.create",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="checkout_session",
        resource_id=session_data["session_id"],
        reason=str(body.amount),
    )
    await db.commit()
    return CheckoutSessionOut(
        session_id=session_data["session_id"],
        url=session_data["url"],
        amount=session_data["amount"],
        currency=session_data["currency"],
    )


@router.post("/billing/webhooks/stripe", response_model=StripeWebhookOut)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(default="", alias="Stripe-Signature"),
    db: AsyncSession = Depends(get_db),
) -> StripeWebhookOut:
    """Public webhook receiver for signed Stripe events.

    Verifies the HMAC signature against STRIPE_WEBHOOK_SECRET, then posts an
    idempotent credit_purchase ledger transaction keyed by the Stripe event id.
    """
    raw_body = await request.body()
    provider = StripePaymentProvider()
    try:
        result = await provider.handle_webhook_event(
            db,
            payload=raw_body,
            signature_header=stripe_signature,
        )
    except StripeSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message)
    except Exception as exc:
        logger.error("Error processing Stripe webhook: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="webhook_processing_error"
        )

    await db.commit()
    return StripeWebhookOut(
        received=True,
        event_id=result.get("event_id", ""),
        event_type=result.get("event_type"),
        status=result.get("status", "ok"),
        transaction_id=result.get("transaction_id"),
    )



@router.get("/billing/ledger", response_model=LedgerPageOut)
async def get_ledger(
    page: int = Query(default=1, ge=1, le=10_000_000),
    page_size: int = Query(default=50, ge=1, le=200),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LedgerPageOut:
    account = await ledger.get_or_create_customer_account(db, current.id)
    total = (
        await db.execute(
            select(func.count())
            .select_from(LedgerEntry)
            .where(LedgerEntry.account_id == account.id)
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(LedgerEntry, LedgerTransaction)
            .join(LedgerTransaction, LedgerEntry.transaction_id == LedgerTransaction.id)
            .where(LedgerEntry.account_id == account.id)
            .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    await db.commit()
    return LedgerPageOut(
        items=[_shape_entry(e, t) for e, t in rows],
        page=page,
        page_size=page_size,
        total=int(total),
    )


@router.get("/usage", response_model=UsageSummaryOut)
async def get_usage(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UsageSummaryOut:
    rows = (
        await db.execute(
            select(UsageRecord)
            .where(UsageRecord.customer_id == current.id)
            .order_by(UsageRecord.created_at.desc())
        )
    ).scalars().all()
    total_est = sum(r.estimated_cost_minor for r in rows)
    total_act = sum(r.actual_cost_minor for r in rows)
    return UsageSummaryOut(
        total_estimated=ledger.minor_to_str(total_est),
        total_actual=ledger.minor_to_str(total_act),
        records=[
            UsageRecordOut(
                job_id=r.job_id,
                estimated_minutes=r.estimated_minutes,
                actual_seconds=r.actual_seconds,
                estimated_cost=ledger.minor_to_str(r.estimated_cost_minor),
                actual_cost=ledger.minor_to_str(r.actual_cost_minor),
                currency=r.currency,
                created_at=r.created_at,
            )
            for r in rows
        ],
    )
