"""Provider service helpers: lookups, machine response shaping, status derivation."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.models.provider import Provider, ProviderMachine
from app.schemas.provider import HardwareOut, MachineDetailOut, MachineOut


async def get_provider_by_user(db: AsyncSession, user_id: uuid.UUID) -> Provider | None:
    result = await db.execute(select(Provider).where(Provider.user_id == user_id))
    return result.scalar_one_or_none()


def _machine_query():
    return select(ProviderMachine).options(
        selectinload(ProviderMachine.hardware),
        selectinload(ProviderMachine.capability),
    )


async def get_owned_machine(
    db: AsyncSession, provider_id: uuid.UUID, machine_id: uuid.UUID
) -> ProviderMachine | None:
    """Fetch a non-deregistered machine owned by ``provider_id`` (else None)."""
    result = await db.execute(
        _machine_query().where(
            ProviderMachine.id == machine_id,
            ProviderMachine.provider_id == provider_id,
            ProviderMachine.status != "deregistered",
        )
    )
    return result.scalar_one_or_none()


async def list_provider_machines(
    db: AsyncSession, provider_id: uuid.UUID
) -> list[ProviderMachine]:
    result = await db.execute(
        _machine_query()
        .where(
            ProviderMachine.provider_id == provider_id,
            ProviderMachine.status != "deregistered",
        )
        .order_by(ProviderMachine.created_at)
    )
    return list(result.scalars().all())


def derive_status(machine: ProviderMachine, now: dt.datetime | None = None) -> str:
    """Derive the public status from availability + heartbeat freshness."""
    now = now or utcnow()
    if machine.availability == "maintenance":
        return "maintenance"
    last = ensure_aware(machine.last_heartbeat_at)
    fresh = last is not None and (now - last).total_seconds() <= settings.ONLINE_WINDOW_SECONDS
    if machine.availability == "online" and fresh:
        return "online"
    return "offline"


def _price_str(value: decimal.Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{decimal.Decimal(value):.2f}"


def _hardware_out(machine: ProviderMachine) -> HardwareOut:
    hw = machine.hardware
    if hw is None:
        return HardwareOut()
    return HardwareOut(
        gpu_model=hw.gpu_model,
        gpu_count=hw.gpu_count,
        vram_mb=hw.vram_mb,
        cpu_model=hw.cpu_model,
        cpu_cores=hw.cpu_cores,
        ram_mb=hw.ram_mb,
        disk_gb=hw.disk_gb,
        cuda_version=hw.cuda_version,
    )


def _workloads(machine: ProviderMachine) -> list[str]:
    if machine.capability is None or not machine.capability.supported_workloads:
        return []
    return list(machine.capability.supported_workloads)


def shape_machine(machine: ProviderMachine) -> MachineOut:
    return MachineOut(
        machine_id=machine.id,
        hostname=machine.hostname,
        agent_version=machine.agent_version,
        status=derive_status(machine),
        availability=machine.availability,
        verification_status=(
            machine.hardware.verification_status if machine.hardware else "detected"
        ),
        asking_price_per_hour=_price_str(machine.asking_price_per_hour),
        max_concurrent_jobs=machine.max_concurrent_jobs,
        region=machine.region,
        last_heartbeat_at=ensure_aware(machine.last_heartbeat_at),
        hardware=_hardware_out(machine),
        supported_workloads=_workloads(machine),
        created_at=machine.created_at,
    )


def shape_machine_detail(machine: ProviderMachine) -> MachineDetailOut:
    base = shape_machine(machine)
    return MachineDetailOut(**base.model_dump(), last_health=machine.last_health)
