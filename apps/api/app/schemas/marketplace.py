"""Marketplace / quotes / reservations request & response schemas (Phase 3).

Money is always serialized as a 2dp decimal STRING (never a float). We store
Decimal internally and convert to string at the schema boundary.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from pydantic import BaseModel, Field

CURRENCY = "USDC_CREDITS"


def money_str(value: decimal.Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{decimal.Decimal(value).quantize(decimal.Decimal('0.01'), rounding=decimal.ROUND_HALF_UP)}"


# ---------------------------------------------------------------------------
# Marketplace search
# ---------------------------------------------------------------------------
class ReliabilityOut(BaseModel):
    completed_jobs: int = 0
    success_rate: float | None = None  # null until completed-job history exists


class MarketplaceMachineOut(BaseModel):
    machine_id: uuid.UUID
    gpu_model: str | None = None
    gpu_vendor: str | None = None
    gpu_runtime: str | None = None
    gpu_count: int = 0
    vram_mb: int = 0
    cpu_model: str | None = None
    cpu_cores: int = 0
    ram_mb: int = 0
    region: str | None = None
    verification_status: str = "detected"
    price_per_hour: str
    currency: str = CURRENCY
    supported_workloads: list[str] = Field(default_factory=list)
    available_slots: int = 0
    max_concurrent_jobs: int = 1
    reliability: ReliabilityOut = Field(default_factory=ReliabilityOut)
    last_heartbeat_at: dt.datetime | None = None
    # "dedicated" (a server nobody else uses), "shared" (a PC its owner also uses: the job may slow down or be
    # interrupted) or "unspecified". Declared by the host; Windows hosts are always shared.
    host_kind: str = "unspecified"


class MarketplaceListOut(BaseModel):
    items: list[MarketplaceMachineOut]
    page: int
    page_size: int
    total: int


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------
class QuoteCreate(BaseModel):
    machine_id: uuid.UUID
    workload_type: str = Field(min_length=1, max_length=64)
    estimated_minutes: int = Field(gt=0, le=525_600)
    gpu_count: int = Field(default=1, ge=1, le=64)
    # max_digits bounds the exponent too: "1e400" used to overflow Decimal arithmetic (HTTP 500).
    # le: quotes.max_authorized_spend is NUMERIC(14, 2); a larger value was accepted here and then failed at INSERT time
    # on Postgres (HTTP 500). 10**12 is already far beyond any quote total (a maximal quote is under 7e11).
    max_authorized_spend: decimal.Decimal | None = Field(default=None, max_digits=24, lt=10**12)


class QuoteBreakdownOut(BaseModel):
    price_per_hour: str
    estimated_hours: str
    gpu_cost: str
    platform_fee_rate: str
    platform_fee: str
    total: str


class QuoteOut(BaseModel):
    quote_id: uuid.UUID
    machine_id: uuid.UUID
    customer_id: uuid.UUID
    workload_type: str
    gpu_count: int
    estimated_minutes: int
    currency: str = CURRENCY
    breakdown: QuoteBreakdownOut
    max_authorized_spend: str | None = None
    status: str
    expires_at: dt.datetime
    created_at: dt.datetime


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------
class ReservationOut(BaseModel):
    reservation_id: uuid.UUID
    quote_id: uuid.UUID
    machine_id: uuid.UUID
    slots: int
    status: str
    expires_at: dt.datetime
    created_at: dt.datetime


class ReservationReleaseOut(BaseModel):
    reservation_id: uuid.UUID
    status: str
