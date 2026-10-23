"""Batch submission + batch pricing (Phase C1).

A batch is a GROUPING over the existing single-job pipeline, never a second
pipeline: every child is created by calling the very same
``POST /v1/jobs`` handler (``app.api.routers.jobs.create_job``), so the FSM,
validation, budget hold and scheduling behave identically and can never drift.
Each child therefore bills ITSELF through the normal ledger path — a batch row
holds no money and is never invoiced, so a partially-finished batch cannot be
billed as if it had completed.

Bulk discount
-------------
``settings.BATCH_DISCOUNT_PCT`` is applied to each child's quote by discounting
``price_per_hour_snapshot`` and recomputing the breakdown with the SAME pricing
function used everywhere else. Discounting the hourly snapshot (rather than only
the total) means settlement — which meters actual seconds against that snapshot
— charges the discounted rate too.

WHY the hold is left at the undiscounted amount: the hold is placed inside
``schedule_job`` before this service can see the quote, and it is an
authorization CEILING, not a charge. Settlement captures the metered discounted
cost and releases the remainder, so the customer is never overcharged — only
slightly over-authorized while the job runs.
"""

from __future__ import annotations

import decimal
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.jobs import cancel_job, create_job
from app.core.config import settings
from app.db.base import utcnow
from app.models.jobs import Job
from app.models.marketplace import CURRENCY, Quote, ResourceReservation
from app.models.workloads import Batch
from app.schemas.auth import CurrentUser
from app.schemas.batch import BatchItemOut, BatchQuoteOut
from app.schemas.jobs import JobCreate
from app.schemas.marketplace import money_str
from app.services import job_fsm as fsm
from app.services.pricing import compute_breakdown

_CENTS = decimal.Decimal("0.01")

# Child states that mean "this item is done, one way or another".
TERMINAL_STATES = fsm.TERMINAL_STATES


def _money(value: decimal.Decimal) -> decimal.Decimal:
    return decimal.Decimal(value).quantize(_CENTS, rounding=decimal.ROUND_HALF_UP)


def discount_factor(pct: decimal.Decimal) -> decimal.Decimal:
    """``1 - pct/100`` clamped to [0, 1]. Decimal only — money is never float."""
    pct = max(decimal.Decimal(0), min(decimal.Decimal(100), decimal.Decimal(pct)))
    return (decimal.Decimal(100) - pct) / decimal.Decimal(100)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
async def find_batch_by_key(
    db: AsyncSession, customer_id: uuid.UUID, key: str
) -> Batch | None:
    result = await db.execute(
        select(Batch).where(Batch.customer_id == customer_id, Batch.idempotency_key == key)
    )
    return result.scalar_one_or_none()


async def get_own_batch(
    db: AsyncSession, customer_id: uuid.UUID, batch_id: uuid.UUID
) -> Batch | None:
    """Owner-scoped fetch — a non-owner gets ``None`` (the router answers 404)."""
    result = await db.execute(
        select(Batch).where(Batch.id == batch_id, Batch.customer_id == customer_id)
    )
    return result.scalar_one_or_none()


async def child_jobs(db: AsyncSession, batch_id: uuid.UUID) -> list[Job]:
    result = await db.execute(
        select(Job).where(Job.batch_id == batch_id).order_by(Job.created_at, Job.id)
    )
    return list(result.scalars().all())


async def _job_by_idempotency_key(
    db: AsyncSession, customer_id: uuid.UUID, key: str
) -> Job | None:
    result = await db.execute(
        select(Job).where(Job.customer_id == customer_id, Job.idempotency_key == key)
    )
    return result.scalar_one_or_none()


async def create_batch(
    db: AsyncSession,
    *,
    request,
    current: CurrentUser,
    items: list[JobCreate],
    idempotency_key: str | None,
) -> Batch:
    """Create a batch and submit every item through the normal job pipeline.

    Returns the existing batch untouched when ``idempotency_key`` replays, so a
    retried submission never duplicates jobs or holds.
    """
    if idempotency_key:
        prior = await find_batch_by_key(db, current.id, idempotency_key)
        if prior is not None:
            return prior

    if not items:
        raise HTTPException(status_code=422, detail="batch_empty")
    if len(items) > settings.BATCH_MAX_ITEMS:
        raise HTTPException(status_code=422, detail="batch_too_large")

    batch = Batch(
        customer_id=current.id,
        status="created",
        total_items=len(items),
        discount_pct=settings.batch_discount_pct,
        idempotency_key=idempotency_key,
    )
    db.add(batch)
    await db.flush()

    for index, item in enumerate(items):
        await _submit_child(db, request=request, current=current, batch=batch, index=index, item=item)

    await refresh_counts(db, batch)
    return batch


async def _submit_child(
    db: AsyncSession,
    *,
    request,
    current: CurrentUser,
    batch: Batch,
    index: int,
    item: JobCreate,
) -> None:
    """Submit one item via the single-job handler and adopt it into the batch.

    A per-item failure (bad input, no credit, ...) does NOT abort the batch: the
    failed child is still adopted so the batch's counts stay truthful.
    """
    key = f"batch:{batch.id}:{index}"
    try:
        created = await create_job(
            body=item,
            request=request,
            idempotency_key=key,
            current=current,
            db=db,
        )
    except HTTPException:
        # create_job persists (and commits) the FAILED job before raising, so the
        # child exists and can be found by its derived idempotency key.
        failed = await _job_by_idempotency_key(db, current.id, key)
        if failed is not None:
            failed.batch_id = batch.id
            await db.flush()
        return

    job = await db.get(Job, created.job_id)
    if job is None:  # pragma: no cover - defensive; create_job just persisted it
        return
    job.batch_id = batch.id
    await apply_batch_discount(db, job, decimal.Decimal(batch.discount_pct))
    await db.flush()


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
async def _quote_for_job(db: AsyncSession, job: Job) -> Quote | None:
    if job.reservation_id is None:
        return None
    reservation = await db.get(ResourceReservation, job.reservation_id)
    if reservation is None:
        return None
    return await db.get(Quote, reservation.quote_id)


async def apply_batch_discount(
    db: AsyncSession, job: Job, pct: decimal.Decimal
) -> Quote | None:
    """Apply the bulk discount to a child's quote, in place.

    Idempotent in practice because it is called exactly once per child, right
    after creation; a zero percent discount is a no-op.
    """
    pct = decimal.Decimal(pct)
    if pct <= 0:
        return None
    quote = await _quote_for_job(db, job)
    if quote is None:
        return None
    factor = discount_factor(pct)
    discounted_price = _money(decimal.Decimal(quote.price_per_hour_snapshot) * factor)
    breakdown = compute_breakdown(
        price_per_hour=discounted_price,
        estimated_minutes=quote.estimated_minutes,
        gpu_count=quote.gpu_count,
        platform_fee_rate=decimal.Decimal(quote.platform_fee_rate),
    )
    quote.price_per_hour_snapshot = discounted_price
    quote.gpu_cost = breakdown.gpu_cost
    quote.platform_fee = breakdown.platform_fee
    quote.total = breakdown.total
    await db.flush()
    return quote


async def quote_summary(
    db: AsyncSession, batch: Batch, jobs: list[Job]
) -> BatchQuoteOut:
    """Summarize what the batch's priced children cost, with the discount shown.

    ``discounted_total`` is the sum of the children's REAL quote totals. The
    undiscounted figure is reconstructed by re-inflating each discounted hourly
    snapshot by the same factor that was applied, which is exact for the
    disclosed percent (rounding can move it by at most a cent per item).
    """
    pct = decimal.Decimal(batch.discount_pct)
    factor = discount_factor(pct)
    discounted = decimal.Decimal(0)
    undiscounted = decimal.Decimal(0)
    priced = 0
    for job in jobs:
        quote = await _quote_for_job(db, job)
        if quote is None:
            continue
        priced += 1
        discounted += decimal.Decimal(quote.total)
        if factor <= 0:
            continue
        original_price = decimal.Decimal(quote.price_per_hour_snapshot) / factor
        original = compute_breakdown(
            price_per_hour=original_price,
            estimated_minutes=quote.estimated_minutes,
            gpu_count=quote.gpu_count,
            platform_fee_rate=decimal.Decimal(quote.platform_fee_rate),
        )
        undiscounted += original.total
    discounted_q = _money(discounted)
    undiscounted_q = _money(undiscounted)
    return BatchQuoteOut(
        currency=CURRENCY,
        priced_items=priced,
        discount_pct=f"{decimal.Decimal(pct).quantize(_CENTS)}",
        undiscounted_total=money_str(undiscounted_q) or "0.00",
        discount_amount=money_str(_money(undiscounted_q - discounted_q)) or "0.00",
        discounted_total=money_str(discounted_q) or "0.00",
    )


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def derive_status(jobs: list[Job], total_items: int) -> str:
    """Derive the batch state from its children.

    A batch is ``completed`` ONLY when every declared item finished successfully
    — any failure, cancellation or missing child makes it ``failed``/``cancelled``
    so a partial batch is never presented (or billed) as a complete one.
    """
    if not jobs:
        return "created"
    pending = [j for j in jobs if j.status not in TERMINAL_STATES]
    if pending:
        return "created" if all(j.status == fsm.CREATED for j in jobs) else "running"
    completed = sum(1 for j in jobs if j.status == fsm.COMPLETED)
    cancelled = sum(1 for j in jobs if j.status == fsm.CANCELLED)
    failed = len(jobs) - completed - cancelled
    if failed or len(jobs) < total_items:
        return "failed"
    if cancelled:
        return "cancelled"
    return "completed"


async def refresh_counts(db: AsyncSession, batch: Batch) -> list[Job]:
    """Recompute the batch's cached counters + status from its children."""
    jobs = await child_jobs(db, batch.id)
    batch.completed_items = sum(1 for j in jobs if j.status == fsm.COMPLETED)
    batch.failed_items = sum(
        1 for j in jobs if j.status in (fsm.FAILED, fsm.TIMED_OUT, fsm.DISPUTED)
    )
    batch.status = derive_status(jobs, batch.total_items)
    if batch.status in ("completed", "failed", "cancelled"):
        if batch.finished_at is None:
            batch.finished_at = utcnow()
    else:
        batch.finished_at = None
    await db.flush()
    return jobs


def item_views(jobs: list[Job]) -> list[BatchItemOut]:
    return [
        BatchItemOut(
            index=index,
            job_id=job.id,
            status=job.status,
            error_code=job.error_code,
            final_cost=money_str(job.final_cost),
        )
        for index, job in enumerate(jobs)
    ]


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
async def cancel_remaining(
    db: AsyncSession, *, request, current: CurrentUser, batch: Batch
) -> tuple[int, int]:
    """Cancel only the children that are still cancellable.

    Reuses the single-job cancel handler so the hold-release and FSM edge are
    identical. Returns ``(cancelled, skipped_terminal)``.
    """
    jobs = await child_jobs(db, batch.id)
    cancelled = 0
    skipped = 0
    for job in jobs:
        if job.status in TERMINAL_STATES:
            skipped += 1
            continue
        await cancel_job(job_id=job.id, request=request, current=current, db=db)
        cancelled += 1
    await refresh_counts(db, batch)
    return cancelled, skipped
