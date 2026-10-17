"""Marketplace service: public search, quote creation, atomic reservations.

CAPACITY INVARIANT (contract §Capacity rule)
--------------------------------------------
used_slots(machine) = SUM(reservations.slots)
    WHERE machine_id=? AND status='active' AND expires_at > now()
A reservation of ``slots`` is allowed iff ``used + slots <= max_concurrent_jobs``.

The check-and-insert MUST be atomic so two concurrent requests for the last slot
yield exactly one success and one 409.

LOCKING — SQLite vs Postgres (honest note)
------------------------------------------
On Postgres the machine row is locked with ``SELECT ... FOR UPDATE`` so the
recount-and-insert serializes across *processes*. SQLite does NOT honor
``FOR UPDATE`` (it is a no-op), so in-process concurrency (our test harness, which
shares a single connection via StaticPool) is serialized here with a per-machine
``asyncio.Lock``. Both guards wrap the SAME transactional recount+insert, and the
UNIQUE(quote_id) constraint independently guarantees one reservation per quote.
The asyncio.Lock only protects a single process; true multi-worker safety in prod
comes from the row lock on Postgres.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import uuid
from collections import defaultdict

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.models.marketplace import CURRENCY, Quote, ResourceReservation
from app.models.provider import ProviderMachine
from app.schemas.marketplace import (
    MarketplaceMachineOut,
    ReliabilityOut,
    money_str,
)
from app.services.provider import derive_status

# Per-machine in-process locks (see module docstring on SQLite vs Postgres).
_machine_locks: dict[uuid.UUID, asyncio.Lock] = {}


def _machine_lock(machine_id: uuid.UUID) -> asyncio.Lock:
    lock = _machine_locks.get(machine_id)
    if lock is None:
        lock = asyncio.Lock()
        _machine_locks[machine_id] = lock
    return lock


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------
async def active_reservation_slots(
    db: AsyncSession, machine_id: uuid.UUID, now: dt.datetime | None = None
) -> int:
    """SUM of active, non-expired reservation slots for a machine (lazy expiry)."""
    now = now or utcnow()
    result = await db.execute(
        select(ResourceReservation).where(
            ResourceReservation.machine_id == machine_id,
            ResourceReservation.status == "active",
        )
    )
    used = 0
    for res in result.scalars().all():
        if ensure_aware(res.expires_at) is not None and ensure_aware(res.expires_at) > now:
            used += res.slots
    return used


async def _used_slots_by_machine(
    db: AsyncSession, machine_ids: list[uuid.UUID], now: dt.datetime
) -> dict[uuid.UUID, int]:
    if not machine_ids:
        return {}
    result = await db.execute(
        select(ResourceReservation).where(
            ResourceReservation.machine_id.in_(machine_ids),
            ResourceReservation.status == "active",
        )
    )
    used: dict[uuid.UUID, int] = defaultdict(int)
    for res in result.scalars().all():
        exp = ensure_aware(res.expires_at)
        if exp is not None and exp > now:
            used[res.machine_id] += res.slots
    return used


# ---------------------------------------------------------------------------
# Public search
# ---------------------------------------------------------------------------
def _listable(machine: ProviderMachine, now: dt.datetime) -> bool:
    """A machine is listable iff online+available+priced+not-deregistered."""
    if machine.status == "deregistered":
        return False
    if machine.asking_price_per_hour is None:
        return False
    if machine.availability != "online":
        return False
    return derive_status(machine, now) == "online"


def _matches_filters(
    machine: ProviderMachine,
    *,
    gpu_model: str | None,
    min_vram_mb: int | None,
    max_price_per_hour: decimal.Decimal | None,
    region: str | None,
    workload: str | None,
    verification_status: str | None,
) -> bool:
    hw = machine.hardware
    if gpu_model is not None:
        model = (hw.gpu_model if hw else None) or ""
        if gpu_model.lower() not in model.lower():
            return False
    if min_vram_mb is not None:
        vram = hw.vram_mb if hw else 0
        if vram < min_vram_mb:
            return False
    if max_price_per_hour is not None:
        if decimal.Decimal(machine.asking_price_per_hour) > decimal.Decimal(max_price_per_hour):
            return False
    if region is not None and (machine.region or "") != region:
        return False
    if verification_status is not None:
        vs = (hw.verification_status if hw else "detected")
        if vs != verification_status:
            return False
    if workload is not None:
        workloads = machine.capability.supported_workloads if machine.capability else []
        if workload not in (workloads or []):
            return False
    return True


def _supported_workloads(machine: ProviderMachine) -> list[str]:
    if machine.capability is None or not machine.capability.supported_workloads:
        return []
    return list(machine.capability.supported_workloads)


def shape_marketplace_machine(
    machine: ProviderMachine, available_slots: int
) -> MarketplaceMachineOut:
    hw = machine.hardware
    return MarketplaceMachineOut(
        machine_id=machine.id,
        gpu_model=hw.gpu_model if hw else None,
        gpu_count=hw.gpu_count if hw else 0,
        vram_mb=hw.vram_mb if hw else 0,
        cpu_model=hw.cpu_model if hw else None,
        cpu_cores=hw.cpu_cores if hw else 0,
        ram_mb=hw.ram_mb if hw else 0,
        region=machine.region,
        verification_status=(hw.verification_status if hw else "detected"),
        price_per_hour=money_str(machine.asking_price_per_hour),
        currency=CURRENCY,
        supported_workloads=_supported_workloads(machine),
        available_slots=available_slots,
        max_concurrent_jobs=machine.max_concurrent_jobs,
        # Honest: no completed-job history yet, so success_rate is null.
        reliability=ReliabilityOut(completed_jobs=0, success_rate=None),
        last_heartbeat_at=ensure_aware(machine.last_heartbeat_at),
    )


def _base_machine_query():
    return select(ProviderMachine).options(
        selectinload(ProviderMachine.hardware),
        selectinload(ProviderMachine.capability),
    )


async def list_marketplace_machines(
    db: AsyncSession,
    *,
    gpu_model: str | None = None,
    min_vram_mb: int | None = None,
    max_price_per_hour: decimal.Decimal | None = None,
    region: str | None = None,
    workload: str | None = None,
    verification_status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[MarketplaceMachineOut], int]:
    now = utcnow()
    result = await db.execute(
        _base_machine_query()
        .where(
            ProviderMachine.status != "deregistered",
            ProviderMachine.availability == "online",
            ProviderMachine.asking_price_per_hour.is_not(None),
        )
        .order_by(ProviderMachine.created_at)
    )
    candidates = [m for m in result.scalars().all() if _listable(m, now)]
    filtered = [
        m
        for m in candidates
        if _matches_filters(
            m,
            gpu_model=gpu_model,
            min_vram_mb=min_vram_mb,
            max_price_per_hour=max_price_per_hour,
            region=region,
            workload=workload,
            verification_status=verification_status,
        )
    ]
    total = len(filtered)
    start = (page - 1) * page_size
    window = filtered[start : start + page_size]

    used = await _used_slots_by_machine(db, [m.id for m in window], now)
    items = [
        shape_marketplace_machine(
            m, max(0, m.max_concurrent_jobs - used.get(m.id, 0))
        )
        for m in window
    ]
    return items, total


async def get_listable_machine(
    db: AsyncSession, machine_id: uuid.UUID
) -> ProviderMachine | None:
    result = await db.execute(_base_machine_query().where(ProviderMachine.id == machine_id))
    machine = result.scalar_one_or_none()
    if machine is None or not _listable(machine, utcnow()):
        return None
    return machine


# ---------------------------------------------------------------------------
# Reservation (atomic)
# ---------------------------------------------------------------------------
async def reserve_capacity(
    db: AsyncSession,
    *,
    quote: Quote,
    now: dt.datetime | None = None,
) -> ResourceReservation:
    """Atomically reserve a capacity slot for ``quote``.

    Raises HTTPException(409) with detail 'no_capacity' when the machine is full.
    Caller has already validated ownership, quote freshness and no existing
    reservation (though the UNIQUE(quote_id) is the final backstop).
    """
    now = now or utcnow()
    lock = _machine_lock(quote.machine_id)
    async with lock:
        # Lock the machine row. On Postgres this blocks concurrent reservers;
        # on SQLite it is a no-op (the asyncio.Lock above provides the fence).
        machine = (
            await db.execute(
                select(ProviderMachine)
                .where(ProviderMachine.id == quote.machine_id)
                .with_for_update()
            )
        ).scalar_one()

        used = await active_reservation_slots(db, quote.machine_id, now)
        if used + quote.gpu_count > machine.max_concurrent_jobs:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="no_capacity"
            )

        reservation = ResourceReservation(
            quote_id=quote.id,
            machine_id=quote.machine_id,
            customer_id=quote.customer_id,
            slots=quote.gpu_count,
            status="active",
            expires_at=now + dt.timedelta(seconds=settings.RESERVATION_TTL_SECONDS),
        )
        db.add(reservation)
        quote.status = "reserved"
        try:
            await db.flush()
        except IntegrityError:
            # UNIQUE(quote_id) backstop — a reservation already exists.
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="quote_already_reserved"
            )
        return reservation


def derive_reservation_status(
    reservation: ResourceReservation, now: dt.datetime | None = None
) -> str:
    """Derived status for list views (active may be lazily expired)."""
    now = now or utcnow()
    if reservation.status != "active":
        return reservation.status
    exp = ensure_aware(reservation.expires_at)
    if exp is not None and exp <= now:
        return "expired"
    return "active"
