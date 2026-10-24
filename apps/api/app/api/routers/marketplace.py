"""Public marketplace search endpoints (NO auth).

Exposes capability + price + availability for real, online, available, priced
machines. Never exposes provider PII, secrets or owner identity.
"""

from __future__ import annotations

import decimal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.schemas.marketplace import MarketplaceListOut, MarketplaceMachineOut
from app.services.marketplace import (
    active_reservation_slots,
    get_listable_machine,
    list_marketplace_machines,
    shape_marketplace_machine,
)

router = APIRouter(prefix="/v1/marketplace", tags=["marketplace"])


@router.get("/machines", response_model=MarketplaceListOut)
async def search_machines(
    gpu_model: str | None = Query(default=None),
    min_vram_mb: int | None = Query(default=None, ge=0),
    max_price_per_hour: decimal.Decimal | None = Query(default=None, ge=0),
    region: str | None = Query(default=None),
    workload: str | None = Query(default=None),
    verification_status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1, le=10_000_000),
    page_size: int | None = Query(default=None, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> MarketplaceListOut:
    size = page_size or settings.MARKETPLACE_PAGE_SIZE_DEFAULT
    size = min(size, settings.MARKETPLACE_PAGE_SIZE_MAX)
    items, total = await list_marketplace_machines(
        db,
        gpu_model=gpu_model,
        min_vram_mb=min_vram_mb,
        max_price_per_hour=max_price_per_hour,
        region=region,
        workload=workload,
        verification_status=verification_status,
        page=page,
        page_size=size,
    )
    return MarketplaceListOut(items=items, page=page, page_size=size, total=total)


@router.get("/machines/{machine_id}", response_model=MarketplaceMachineOut)
async def get_machine(
    machine_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> MarketplaceMachineOut:
    machine = await get_listable_machine(db, machine_id)
    if machine is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found"
        )
    used = await active_reservation_slots(db, machine.id)
    available = max(0, machine.max_concurrent_jobs - used)
    return shape_marketplace_machine(machine, available)
