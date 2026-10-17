"""Pricing service — pure Decimal math per the Phase 3 contract.

```
estimated_hours = estimated_minutes / 60
gpu_cost        = price_per_hour_snapshot * estimated_hours * gpu_count
platform_fee    = gpu_cost * platform_fee_rate
total           = gpu_cost + platform_fee
```

All money is quantized to 2dp with ROUND_HALF_UP. The active ``platform_fee_rate``
is read from the single active ``pricing_rules`` row (seeded default 0.10).
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.marketplace import PricingRule

_CENTS = decimal.Decimal("0.01")


def _money(value: decimal.Decimal) -> decimal.Decimal:
    """Quantize a money amount to 2dp, ROUND_HALF_UP."""
    return decimal.Decimal(value).quantize(_CENTS, rounding=decimal.ROUND_HALF_UP)


@dataclass(frozen=True)
class PriceBreakdown:
    price_per_hour: decimal.Decimal
    estimated_hours: decimal.Decimal
    gpu_cost: decimal.Decimal
    platform_fee_rate: decimal.Decimal
    platform_fee: decimal.Decimal
    total: decimal.Decimal


def compute_breakdown(
    *,
    price_per_hour: decimal.Decimal,
    estimated_minutes: int,
    gpu_count: int,
    platform_fee_rate: decimal.Decimal,
) -> PriceBreakdown:
    """Deterministic pricing math. Inputs/outputs are ``Decimal`` only."""
    price = decimal.Decimal(price_per_hour)
    rate = decimal.Decimal(platform_fee_rate)
    # Keep full precision through the computation; quantize money lines only.
    estimated_hours = (decimal.Decimal(estimated_minutes) / decimal.Decimal(60))
    gpu_cost = price * estimated_hours * decimal.Decimal(gpu_count)
    gpu_cost_q = _money(gpu_cost)
    platform_fee_q = _money(gpu_cost_q * rate)
    total_q = _money(gpu_cost_q + platform_fee_q)
    return PriceBreakdown(
        price_per_hour=_money(price),
        estimated_hours=estimated_hours.quantize(_CENTS, rounding=decimal.ROUND_HALF_UP),
        gpu_cost=gpu_cost_q,
        platform_fee_rate=rate,
        platform_fee=platform_fee_q,
        total=total_q,
    )


async def get_active_fee_rate(db: AsyncSession) -> decimal.Decimal:
    """Read the active platform_fee_rate from pricing_rules (fallback: config)."""
    result = await db.execute(
        select(PricingRule.platform_fee_rate)
        .where(PricingRule.active.is_(True))
        .order_by(PricingRule.created_at.desc())
        .limit(1)
    )
    rate = result.scalar_one_or_none()
    if rate is None:
        return decimal.Decimal(settings.DEFAULT_PLATFORM_FEE_RATE)
    return decimal.Decimal(rate)
