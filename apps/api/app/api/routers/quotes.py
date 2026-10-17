"""Customer quote + reservation endpoints (customer JWT, owner-scoped).

Quotes are itemized, time-limited, price-snapshotted cost estimates. Converting a
quote to a reservation atomically holds a capacity slot (see services.marketplace).
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.db.session import get_db
from app.deps import client_ip, get_current_user
from app.models.marketplace import CURRENCY, Quote, ResourceReservation
from app.schemas.auth import CurrentUser
from app.schemas.marketplace import (
    QuoteBreakdownOut,
    QuoteCreate,
    QuoteOut,
    ReservationOut,
    money_str,
)
from app.services.audit import record_audit
from app.services.marketplace import get_listable_machine, reserve_capacity
from app.services.pricing import compute_breakdown, get_active_fee_rate

router = APIRouter(prefix="/v1/quotes", tags=["quotes"])

_QUOTE_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found"
)


def _estimated_hours_str(minutes: int) -> str:
    hours = decimal.Decimal(minutes) / decimal.Decimal(60)
    return money_str(hours)


def _fee_rate_str(rate: decimal.Decimal) -> str:
    return money_str(rate)


def shape_quote(quote: Quote) -> QuoteOut:
    return QuoteOut(
        quote_id=quote.id,
        machine_id=quote.machine_id,
        customer_id=quote.customer_id,
        workload_type=quote.workload_type,
        gpu_count=quote.gpu_count,
        estimated_minutes=quote.estimated_minutes,
        currency=quote.currency,
        breakdown=QuoteBreakdownOut(
            price_per_hour=money_str(quote.price_per_hour_snapshot),
            estimated_hours=_estimated_hours_str(quote.estimated_minutes),
            gpu_cost=money_str(quote.gpu_cost),
            platform_fee_rate=_fee_rate_str(quote.platform_fee_rate),
            platform_fee=money_str(quote.platform_fee),
            total=money_str(quote.total),
        ),
        max_authorized_spend=money_str(quote.max_authorized_spend),
        status=quote.status,
        expires_at=quote.expires_at,
        created_at=quote.created_at,
    )


async def _get_own_quote(
    db: AsyncSession, customer_id: uuid.UUID, quote_id: uuid.UUID
) -> Quote | None:
    result = await db.execute(
        select(Quote).where(Quote.id == quote_id, Quote.customer_id == customer_id)
    )
    return result.scalar_one_or_none()


@router.post("", response_model=QuoteOut, status_code=status.HTTP_201_CREATED)
async def create_quote(
    body: QuoteCreate,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QuoteOut:
    machine = await get_listable_machine(db, body.machine_id)
    if machine is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Machine not listable"
        )

    workloads = machine.capability.supported_workloads if machine.capability else []
    if body.workload_type not in (workloads or []):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="workload_not_supported",
        )

    machine_gpus = machine.hardware.gpu_count if machine.hardware else 0
    if body.gpu_count > machine_gpus:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="gpu_count_exceeds_machine",
        )

    fee_rate = await get_active_fee_rate(db)
    breakdown = compute_breakdown(
        price_per_hour=machine.asking_price_per_hour,
        estimated_minutes=body.estimated_minutes,
        gpu_count=body.gpu_count,
        platform_fee_rate=fee_rate,
    )

    if (
        body.max_authorized_spend is not None
        and breakdown.total > decimal.Decimal(body.max_authorized_spend)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="budget_exceeded"
        )

    now = utcnow()
    quote = Quote(
        customer_id=current.id,
        machine_id=machine.id,
        workload_type=body.workload_type,
        gpu_count=body.gpu_count,
        estimated_minutes=body.estimated_minutes,
        price_per_hour_snapshot=decimal.Decimal(machine.asking_price_per_hour),
        platform_fee_rate=fee_rate,
        gpu_cost=breakdown.gpu_cost,
        platform_fee=breakdown.platform_fee,
        total=breakdown.total,
        currency=CURRENCY,
        max_authorized_spend=body.max_authorized_spend,
        status="active",
        expires_at=now + dt.timedelta(seconds=settings.QUOTE_TTL_SECONDS),
    )
    db.add(quote)
    await db.flush()
    await record_audit(
        db,
        action="quote.create",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="quote",
        resource_id=str(quote.id),
    )
    await db.flush()
    return shape_quote(quote)


@router.get("", response_model=list[QuoteOut])
async def list_quotes(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[QuoteOut]:
    result = await db.execute(
        select(Quote)
        .where(Quote.customer_id == current.id)
        .order_by(Quote.created_at.desc())
    )
    return [shape_quote(q) for q in result.scalars().all()]


@router.get("/{quote_id}", response_model=QuoteOut)
async def get_quote(
    quote_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QuoteOut:
    quote = await _get_own_quote(db, current.id, quote_id)
    if quote is None:
        raise _QUOTE_NOT_FOUND
    return shape_quote(quote)


@router.post(
    "/{quote_id}/reserve",
    response_model=ReservationOut,
    status_code=status.HTTP_201_CREATED,
)
async def reserve_quote(
    quote_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReservationOut:
    quote = await _get_own_quote(db, current.id, quote_id)
    if quote is None:
        raise _QUOTE_NOT_FOUND

    # Already-reserved check first so a second reserve returns the precise error.
    existing = await db.execute(
        select(ResourceReservation).where(ResourceReservation.quote_id == quote.id)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="quote_already_reserved"
        )

    now = utcnow()
    expires = ensure_aware(quote.expires_at)
    if quote.status != "active" or (expires is not None and expires <= now):
        if quote.status == "active" and expires is not None and expires <= now:
            quote.status = "expired"
            await db.flush()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="quote_expired"
        )

    reservation = await reserve_capacity(db, quote=quote, now=now)
    await record_audit(
        db,
        action="reservation.create",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="reservation",
        resource_id=str(reservation.id),
    )
    await db.flush()
    return ReservationOut(
        reservation_id=reservation.id,
        quote_id=reservation.quote_id,
        machine_id=reservation.machine_id,
        slots=reservation.slots,
        status=reservation.status,
        expires_at=reservation.expires_at,
        created_at=reservation.created_at,
    )
