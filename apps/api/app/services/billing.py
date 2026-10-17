"""Billing service (Phase 5) — job-lifecycle money hooks on top of the ledger.

* :func:`reserve_for_job` — HOLD the authorized amount at submit (debit
  customer_credits, credit holding). ``InsufficientFunds`` -> 402 at the edge.
* :func:`settle_job` — on COMPLETED, meter actual usage, CAPTURE the actual cost
  (holding -> provider_earnings + platform_revenue) and RELEASE the remainder back
  to the customer. Idempotent per job (no double charge). Writes usage + invoice.
* :func:`release_hold` — on FAILED/CANCELLED/TIMED_OUT, return the ENTIRE hold to
  the customer. A failed job is never charged.
* :func:`refund` — reverse (part of) a charge, proportionally across
  provider_earnings + platform_revenue, crediting customer_credits.

All arithmetic is integer minor units / Decimal — never float.
"""

from __future__ import annotations

import decimal
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.billing import Invoice, LedgerEntry, LedgerTransaction, UsageRecord
from app.models.jobs import Job
from app.models.marketplace import Quote, ResourceReservation
from app.models.provider import ProviderMachine
from app.services import ledger
from app.services.pricing import compute_breakdown, get_active_fee_rate

_CENTS = decimal.Decimal("0.01")


def _money(value: decimal.Decimal) -> decimal.Decimal:
    return decimal.Decimal(value).quantize(_CENTS, rounding=decimal.ROUND_HALF_UP)


async def _txn_by_key(db: AsyncSession, key: str) -> LedgerTransaction | None:
    return (
        await db.execute(
            select(LedgerTransaction).where(LedgerTransaction.idempotency_key == key)
        )
    ).scalar_one_or_none()


async def _txn_credit_to(db: AsyncSession, txn_id: uuid.UUID, account_id: uuid.UUID) -> int:
    rows = (
        await db.execute(
            select(LedgerEntry.amount_minor).where(
                LedgerEntry.transaction_id == txn_id,
                LedgerEntry.account_id == account_id,
                LedgerEntry.direction == "credit",
            )
        )
    ).scalars().all()
    return sum(int(r) for r in rows)


async def get_hold_minor(db: AsyncSession, job_id: uuid.UUID) -> int:
    """Amount currently held for a job (0 if none). Reads the hold transaction."""
    txn = await _txn_by_key(db, f"hold:{job_id}")
    if txn is None:
        return 0
    # The hold debits customer_credits by the held amount (== credit to holding).
    rows = (
        await db.execute(
            select(LedgerEntry.amount_minor).where(
                LedgerEntry.transaction_id == txn.id,
                LedgerEntry.direction == "debit",
            )
        )
    ).scalars().all()
    return sum(int(r) for r in rows)


async def _quote_for_job(db: AsyncSession, job: Job) -> Quote | None:
    if job.reservation_id is not None:
        reservation = await db.get(ResourceReservation, job.reservation_id)
        if reservation is not None:
            return await db.get(Quote, reservation.quote_id)
    return None


async def _provider_id_for_job(db: AsyncSession, job: Job) -> uuid.UUID | None:
    if job.machine_id is None:
        return None
    machine = await db.get(ProviderMachine, job.machine_id)
    return machine.provider_id if machine is not None else None


# ---------------------------------------------------------------------------
# Hold at submit
# ---------------------------------------------------------------------------
async def reserve_for_job(db: AsyncSession, job: Job, quote: Quote):
    """HOLD the authorized amount (quote total, capped at max_cost) for a job.

    Idempotent per job (idempotency_key ``hold:<job_id>``) so a reschedule never
    double-holds. Raises :class:`ledger.InsufficientFunds` when the customer
    cannot cover the hold (the caller maps this to 402 insufficient_credits).
    """
    total = decimal.Decimal(quote.total)
    if job.max_cost is not None:
        total = min(total, decimal.Decimal(job.max_cost))
    authorized = ledger.to_minor(total)
    if authorized <= 0:
        return None

    customer = await ledger.get_or_create_customer_account(db, job.customer_id)
    holding = await ledger.get_platform_account(db, "holding")
    return await ledger.post_transaction(
        db,
        kind="hold",
        legs=[(customer, "debit", authorized), (holding, "credit", authorized)],
        idempotency_key=f"hold:{job.id}",
        reference_type="job",
        reference_id=str(job.id),
        actor_user_id=job.customer_id,
    )


# ---------------------------------------------------------------------------
# Settle on completion
# ---------------------------------------------------------------------------
def _meter_actual_cost(
    *,
    price_per_hour: decimal.Decimal,
    gpu_count: int,
    fee_rate: decimal.Decimal,
    actual_seconds: decimal.Decimal,
) -> tuple[int, int]:
    """Return (gpu_cost_minor, platform_fee_minor) for the metered usage."""
    actual_hours = actual_seconds / decimal.Decimal(3600)
    gpu_cost = _money(decimal.Decimal(price_per_hour) * actual_hours * decimal.Decimal(gpu_count))
    platform_fee = _money(gpu_cost * decimal.Decimal(fee_rate))
    return ledger.to_minor(gpu_cost), ledger.to_minor(platform_fee)


async def settle_job(db: AsyncSession, job: Job) -> Invoice | None:
    """Settle a COMPLETED job: capture actual cost + release the remainder.

    Idempotent on ``job.id`` — a second call makes no further ledger effect and
    returns the existing invoice.
    """
    existing_invoice = (
        await db.execute(select(Invoice).where(Invoice.job_id == job.id))
    ).scalar_one_or_none()
    if existing_invoice is not None:
        return existing_invoice

    held_minor = await get_hold_minor(db, job.id)
    quote = await _quote_for_job(db, job)
    fee_rate = await get_active_fee_rate(db)

    metrics = job.metrics or {}
    raw_seconds = metrics.get("duration_seconds")
    if raw_seconds is not None:
        actual_seconds = decimal.Decimal(str(raw_seconds))
    else:
        # No metered duration -> fall back to the estimate.
        actual_seconds = decimal.Decimal(job.estimated_minutes) * decimal.Decimal(60)

    # Disclosed billing policy: a completed job is billed for at least a minimum
    # billable duration, so a sub-second (e.g. simulated) job still incurs a
    # minimum charge instead of rounding to zero. Configurable; 0 disables it.
    min_billable = decimal.Decimal(settings.BILLING_MIN_BILLABLE_SECONDS)
    if min_billable > 0:
        actual_seconds = max(actual_seconds, min_billable)

    if quote is not None:
        price_per_hour = decimal.Decimal(quote.price_per_hour_snapshot)
        gpu_count = quote.gpu_count
        fee_rate = decimal.Decimal(quote.platform_fee_rate)
        estimated_total_minor = ledger.to_minor(decimal.Decimal(quote.total))
    else:
        price_per_hour = decimal.Decimal(0)
        gpu_count = job.gpu_count
        estimated_total_minor = held_minor

    gpu_minor, fee_minor = _meter_actual_cost(
        price_per_hour=price_per_hour,
        gpu_count=gpu_count,
        fee_rate=fee_rate,
        actual_seconds=actual_seconds,
    )
    actual_total = gpu_minor + fee_minor

    # Cap the capture at the held amount (never charge beyond the authorization).
    captured = min(actual_total, held_minor)
    if captured == actual_total:
        provider_minor, platform_minor = gpu_minor, fee_minor
    else:
        platform_minor = min(fee_minor, captured)
        provider_minor = captured - platform_minor

    customer = await ledger.get_or_create_customer_account(db, job.customer_id)
    holding = await ledger.get_platform_account(db, "holding")
    platform_rev = await ledger.get_platform_account(db, "platform_revenue")
    provider_id = await _provider_id_for_job(db, job)
    provider_acct = (
        await ledger.get_provider_account(db, provider_id) if provider_id else platform_rev
    )

    # CAPTURE: debit holding -> credit provider_earnings + platform_revenue.
    if captured > 0:
        legs = [(holding, "debit", captured)]
        if provider_minor > 0:
            legs.append((provider_acct, "credit", provider_minor))
        if platform_minor > 0:
            legs.append((platform_rev, "credit", platform_minor))
        # Guard: if rounding left provider+platform != captured, true up platform.
        credited = provider_minor + platform_minor
        if credited != captured:
            legs.append((platform_rev, "credit", captured - credited))
        await ledger.post_transaction(
            db,
            kind="capture",
            legs=legs,
            idempotency_key=f"capture:{job.id}",
            reference_type="job",
            reference_id=str(job.id),
            actor_user_id=job.customer_id,
        )

    # RELEASE the remainder of the hold back to the customer.
    remainder = held_minor - captured
    if remainder > 0:
        await ledger.post_transaction(
            db,
            kind="release",
            legs=[(holding, "debit", remainder), (customer, "credit", remainder)],
            idempotency_key=f"release:{job.id}",
            reference_type="job",
            reference_id=str(job.id),
            actor_user_id=job.customer_id,
        )

    job.final_cost = ledger.from_minor(captured)

    db.add(
        UsageRecord(
            job_id=job.id,
            customer_id=job.customer_id,
            estimated_minutes=job.estimated_minutes,
            actual_seconds=int(actual_seconds),
            estimated_cost_minor=estimated_total_minor,
            actual_cost_minor=captured,
        )
    )
    line_items = [
        {"label": "compute", "amount": ledger.minor_to_str(provider_minor)},
        {"label": "platform_fee", "amount": ledger.minor_to_str(platform_minor)},
    ]
    invoice = Invoice(
        job_id=job.id,
        customer_id=job.customer_id,
        line_items=line_items,
        estimated_total_minor=estimated_total_minor,
        actual_total_minor=captured,
        platform_fee_minor=platform_minor,
        status="paid",
    )
    db.add(invoice)
    await db.flush()
    return invoice


# ---------------------------------------------------------------------------
# Release on terminal failure
# ---------------------------------------------------------------------------
async def release_hold(db: AsyncSession, job: Job) -> None:
    """Return the ENTIRE hold to the customer (failed/cancelled/timed_out).

    Idempotent per job; a no-op when there is no outstanding hold or the job was
    already settled (captured). A failed job is never charged.
    """
    # A settled job must never be released.
    settled = (
        await db.execute(select(Invoice.id).where(Invoice.job_id == job.id))
    ).scalar_one_or_none()
    if settled is not None:
        return
    held_minor = await get_hold_minor(db, job.id)
    if held_minor <= 0:
        return
    customer = await ledger.get_or_create_customer_account(db, job.customer_id)
    holding = await ledger.get_platform_account(db, "holding")
    await ledger.post_transaction(
        db,
        kind="release",
        legs=[(holding, "debit", held_minor), (customer, "credit", held_minor)],
        idempotency_key=f"release_hold:{job.id}",
        reference_type="job",
        reference_id=str(job.id),
        actor_user_id=job.customer_id,
    )


# ---------------------------------------------------------------------------
# Refund
# ---------------------------------------------------------------------------
async def refund(
    db: AsyncSession,
    *,
    job: Job,
    amount_minor: int,
    actor_user_id: uuid.UUID,
    reason: str | None = None,
    idempotency_key: str,
) -> LedgerTransaction:
    """Reverse ``amount_minor`` of a job's charge, proportionally across
    provider_earnings + platform_revenue, crediting customer_credits."""
    capture = await _txn_by_key(db, f"capture:{job.id}")
    if capture is None:
        raise ValueError("no_charge_to_refund")

    customer = await ledger.get_or_create_customer_account(db, job.customer_id)
    platform_rev = await ledger.get_platform_account(db, "platform_revenue")
    provider_id = await _provider_id_for_job(db, job)
    provider_acct = (
        await ledger.get_provider_account(db, provider_id) if provider_id else platform_rev
    )

    captured_platform = await _txn_credit_to(db, capture.id, platform_rev.id)
    captured_provider = await _txn_credit_to(db, capture.id, provider_acct.id)
    captured_total = captured_platform + captured_provider
    if captured_total <= 0 or amount_minor <= 0 or amount_minor > captured_total:
        raise ValueError("invalid_refund_amount")

    # Proportional split (provider share by ratio; platform takes the remainder).
    refund_provider = (amount_minor * captured_provider) // captured_total
    refund_platform = amount_minor - refund_provider

    legs = []
    if refund_provider > 0:
        legs.append((provider_acct, "debit", refund_provider))
    if refund_platform > 0:
        legs.append((platform_rev, "debit", refund_platform))
    legs.append((customer, "credit", amount_minor))

    return await ledger.post_transaction(
        db,
        kind="refund",
        legs=legs,
        idempotency_key=idempotency_key,
        reference_type="job",
        reference_id=str(job.id),
        actor_user_id=actor_user_id,
    )


# ---------------------------------------------------------------------------
# Customer account view helpers
# ---------------------------------------------------------------------------
async def customer_reserved_minor(db: AsyncSession, customer_id: uuid.UUID) -> int:
    """Current holds for a customer = net holding attributable to their jobs.

    Sums holding-account entries across transactions that reference the customer's
    jobs (credit adds, debit subtracts) -> the outstanding reserved amount.
    """
    holding = await ledger.get_platform_account(db, "holding")
    job_ids = (
        await db.execute(select(Job.id).where(Job.customer_id == customer_id))
    ).scalars().all()
    if not job_ids:
        return 0
    ref_ids = {str(j) for j in job_ids}
    rows = (
        await db.execute(
            select(
                LedgerEntry.direction,
                LedgerEntry.amount_minor,
                LedgerTransaction.reference_id,
            )
            .join(LedgerTransaction, LedgerEntry.transaction_id == LedgerTransaction.id)
            .where(
                LedgerEntry.account_id == holding.id,
                LedgerTransaction.reference_type == "job",
            )
        )
    ).all()
    reserved = 0
    for direction, amount, ref_id in rows:
        if ref_id not in ref_ids:
            continue
        reserved += int(amount) if direction == "credit" else -int(amount)
    return max(0, reserved)
