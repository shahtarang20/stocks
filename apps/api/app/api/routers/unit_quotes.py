"""Unit quote routes (U15). OFF unless PRICING_UNITS_ENABLED. Owner-scoped; estimate only (billing is GPU-seconds).

Mounted at /v1/unit-quotes because /v1/quotes is the existing machine-reservation quote API.
"""

from __future__ import annotations

import decimal
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware
from app.db.session import get_db
from app.deps import get_current_user
from app.models.jobs import Job
from app.models.unit_quotes import UnitQuote
from app.schemas.auth import CurrentUser
from app.services import kits as kit_svc
from app.services import unit_quotes as svc

router = APIRouter(prefix="/v1/unit-quotes", tags=["unit-quotes"])


def _require_enabled() -> None:
    if not settings.PRICING_UNITS_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")


class UnitQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kit: str = Field(min_length=1, max_length=100, description="slug@version")
    expected_units: int = Field(ge=1, le=10**9)
    max_cost: decimal.Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    gpu_count: int = Field(default=1, ge=1, le=64)
    price_per_hour: decimal.Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=4)
    egress_gb: decimal.Decimal = Field(default=decimal.Decimal(0), ge=0, le=10**6, max_digits=14, decimal_places=4)
    storage_gb: decimal.Decimal = Field(default=decimal.Decimal(0), ge=0, le=10**6, max_digits=14, decimal_places=4)
    storage_days: int = Field(default=30, ge=0, le=3650)


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: uuid.UUID


class UnitQuoteOut(BaseModel):
    quote_id: uuid.UUID
    kit: str
    unit: str
    expected_units: int
    currency: str
    confidence: str
    capped: bool
    max_cost: str
    expires_at: Any
    job_id: uuid.UUID | None
    quote: dict[str, Any]


def _out(q: UnitQuote) -> UnitQuoteOut:
    return UnitQuoteOut(
        quote_id=q.id, kit=f"{q.kit_slug}@{q.kit_version}", unit=q.unit, expected_units=q.expected_units,
        currency=q.currency, confidence=q.confidence, capped=q.capped, max_cost=str(q.max_cost),
        expires_at=ensure_aware(q.expires_at), job_id=q.job_id, quote=q.detail,
    )


def _raise(exc: svc.UnitQuoteError) -> None:
    raise HTTPException(status_code=exc.http_status, detail=exc.code)


@router.post("", response_model=UnitQuoteOut, status_code=status.HTTP_201_CREATED)
async def create(
    body: UnitQuoteRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UnitQuoteOut:
    _require_enabled()
    slug, sep, version = body.kit.partition("@")
    if not sep or not slug or not version:
        raise HTTPException(status_code=422, detail="kit_must_be_slug_at_version")
    kit = await kit_svc.get_kit(db, slug, version)
    if kit is None:
        raise HTTPException(status_code=404, detail="kit_not_found")
    try:
        q = await svc.create_unit_quote(
            db, customer_id=current.id, kit=kit, expected_units=body.expected_units, gpu_count=body.gpu_count,
            max_cost=body.max_cost, price_per_hour=body.price_per_hour, egress_gb=body.egress_gb,
            storage_gb=body.storage_gb, storage_days=body.storage_days,
        )
    except svc.UnitQuoteError as exc:
        _raise(exc)
    await db.commit()
    return _out(q)


@router.get("/{quote_id}", response_model=UnitQuoteOut)
async def get_one(
    quote_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UnitQuoteOut:
    _require_enabled()
    q = (
        await db.execute(select(UnitQuote).where(UnitQuote.id == quote_id, UnitQuote.customer_id == current.id))
    ).scalar_one_or_none()
    if q is None:
        raise HTTPException(status_code=404, detail="quote_not_found")
    return _out(q)


@router.post("/{quote_id}/link", response_model=UnitQuoteOut)
async def link(
    quote_id: uuid.UUID,
    body: LinkRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UnitQuoteOut:
    _require_enabled()
    job = (
        await db.execute(select(Job).where(Job.id == body.job_id, Job.customer_id == current.id))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    try:
        q = await svc.link_quote_to_job(db, quote_id=quote_id, customer_id=current.id, job=job)
    except svc.UnitQuoteError as exc:
        _raise(exc)
    await db.commit()
    return _out(q)
