"""Unit quotes (U15, ADR-0006). MONEY-ADJACENT: default OFF (PRICING_UNITS_ENABLED), needs owner approval
and independent review before enabling (docs/architecture/U15-pricing-units-design.md).

A quote turns a Kit's declared billing unit (frame, image, minute, 1k tokens...) and its per-unit GPU-seconds
hint into an ESTIMATED price range from the existing GPU-second rate. The customer is always billed metered
GPU-seconds (P5); units are for quoting and display only. Every figure is clamped to the job's max_cost, so a
quote can never show or authorise more than the cap. Nothing here touches the ledger or existing pricing.
"""

from __future__ import annotations

import datetime as dt
import decimal
import statistics
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.unit_quotes import UnitQuote, UnitQuoteSample
from app.services.pricing import get_active_fee_rate

D = decimal.Decimal
_CENTS = D("0.01")
CURRENCY = "CREDITS"
# (low factor, high factor) around the expected value, by confidence.
SPREAD = {"low": (D("0.5"), D("2.0")), "medium": (D("0.7"), D("1.5")), "high": (D("0.85"), D("1.25"))}
MAX_SAMPLES_USED = 200
SAMPLE_RATIO_SANE = (D("0.01"), D("100"))


class UnitQuoteError(Exception):
    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code = code
        self.http_status = status


def _money(v: decimal.Decimal) -> decimal.Decimal:
    return D(v).quantize(_CENTS, rounding=decimal.ROUND_HALF_UP)


def enabled() -> bool:
    return bool(settings.PRICING_UNITS_ENABLED)


def _dec(v: Any, default: str = "0") -> decimal.Decimal:
    try:
        d = D(str(v))
    except (decimal.InvalidOperation, ValueError):
        return D(default)
    return d if d.is_finite() and d >= 0 else D(default)


@dataclass(frozen=True)
class Learned:
    ratio: decimal.Decimal  # multiplier on the hint; 1 when nothing learned
    samples: int
    floor_per_unit: decimal.Decimal | None  # smallest observed actual GPU-seconds per unit


def learned_from_samples(samples: list[tuple[decimal.Decimal, decimal.Decimal, int]]) -> Learned:
    """samples: (estimated_gpu_s, actual_gpu_s, units). Median actual/estimated, bounded; never below observed."""
    ratios: list[decimal.Decimal] = []
    per_unit: list[decimal.Decimal] = []
    for est, act, units in samples:
        est, act = D(est), D(act)
        if est <= 0 or act <= 0 or units <= 0:
            continue
        r = act / est
        if not (SAMPLE_RATIO_SANE[0] <= r <= SAMPLE_RATIO_SANE[1]):
            continue
        ratios.append(r)
        per_unit.append(act / D(units))
    if len(ratios) < int(settings.UNIT_QUOTE_MIN_SAMPLES):
        return Learned(D(1), len(ratios), None)
    lo, hi = _dec(settings.UNIT_QUOTE_RATIO_MIN, "0.5"), _dec(settings.UNIT_QUOTE_RATIO_MAX, "2.0")
    if lo <= 0 or hi < lo:
        lo, hi = D("0.5"), D("2.0")
    median = D(str(statistics.median(ratios)))
    return Learned(max(lo, min(hi, median)), len(ratios), min(per_unit))


async def load_learned(db: AsyncSession, slug: str, version: str, unit: str) -> Learned:
    rows = (
        await db.execute(
            select(UnitQuoteSample.estimated_gpu_seconds, UnitQuoteSample.actual_gpu_seconds, UnitQuoteSample.units)
            .where(
                UnitQuoteSample.kit_slug == slug,
                UnitQuoteSample.kit_version == version,
                UnitQuoteSample.unit == unit,
            )
            .order_by(UnitQuoteSample.created_at.desc())
            .limit(MAX_SAMPLES_USED)
        )
    ).all()
    return learned_from_samples([(r[0], r[1], int(r[2])) for r in rows])


def _confidence(n: int) -> str:
    return "high" if n >= 20 else "medium" if n >= int(settings.UNIT_QUOTE_MIN_SAMPLES) else "low"


def compute_unit_quote(
    *,
    manifest: dict,
    expected_units: int,
    gpu_count: int,
    price_per_hour: decimal.Decimal,
    fee_rate: decimal.Decimal,
    max_cost: decimal.Decimal,
    learned: Learned | None = None,
    egress_gb: decimal.Decimal = D(0),
    storage_gb: decimal.Decimal = D(0),
    storage_days: int = 30,
) -> dict:
    """Pure math. Returns the JSON-able quote body. Raises UnitQuoteError when the Kit declares no usable hint."""
    hints = (manifest or {}).get("billing_hints") or {}
    unit = hints.get("unit")
    per_unit = _dec(hints.get("est_gpu_seconds_per_unit"))
    if not isinstance(unit, str) or per_unit <= 0:
        raise UnitQuoteError("kit_has_no_billing_hint")
    if expected_units < 1 or gpu_count < 1:
        raise UnitQuoteError("invalid_quantity")
    max_cost = D(max_cost)
    if max_cost <= 0:
        raise UnitQuoteError("invalid_max_cost")

    learned = learned or Learned(D(1), 0, None)
    adj_per_unit = per_unit * learned.ratio
    if learned.floor_per_unit is not None:
        adj_per_unit = max(adj_per_unit, learned.floor_per_unit)  # never below what was observed
    conf = _confidence(learned.samples)
    lo_f, hi_f = SPREAD[conf]

    units = D(expected_units)
    exp_s = units * adj_per_unit
    mult = D(price_per_hour) / D(3600) * (D(1) + D(fee_rate))  # credits per GPU-second incl. platform fee

    def cost(seconds: decimal.Decimal) -> decimal.Decimal:
        return _money(seconds * mult)

    lo_raw, exp_raw, hi_raw = cost(exp_s * lo_f), cost(exp_s), cost(exp_s * hi_f)
    capped = hi_raw > max_cost or exp_raw > max_cost
    low, expected, high = (min(x, max_cost) for x in (lo_raw, exp_raw, hi_raw))
    per_unit_price = _money(adj_per_unit * mult) if adj_per_unit * mult >= D("0.005") else (adj_per_unit * mult).quantize(D("0.000001"))

    egress_rate = _dec(settings.UNIT_QUOTE_EGRESS_PER_GB)
    storage_rate = _dec(settings.UNIT_QUOTE_STORAGE_PER_GB_MONTH)
    egress_cost = _money(_dec(egress_gb) * egress_rate)
    storage_cost = _money(_dec(storage_gb) * storage_rate * D(max(0, storage_days)) / D(30))
    return {
        "unit": unit,
        "expected_units": expected_units,
        "gpu_count": gpu_count,
        "estimated_gpu_seconds": str(exp_s.quantize(D("0.01"))),
        "price_per_unit": str(per_unit_price),
        "gpu": {"low": str(low), "expected": str(expected), "high": str(high)},
        "confidence": conf,
        "learned_samples": learned.samples,
        "learned_ratio": str(learned.ratio.quantize(D("0.0001"))),
        "max_cost": str(_money(max_cost)),
        "capped": capped,
        "price_per_hour": str(_money(D(price_per_hour))),
        "platform_fee_rate": str(D(fee_rate)),
        "line_items": [
            {"kind": "egress", "quantity_gb": str(_dec(egress_gb)), "rate_per_gb": str(egress_rate),
             "amount": str(egress_cost), "charged": egress_rate > 0},
            {"kind": "storage", "quantity_gb": str(_dec(storage_gb)), "rate_per_gb_month": str(storage_rate),
             "days": storage_days, "amount": str(storage_cost), "charged": storage_rate > 0},
        ],
        "billing_note": "Estimate only. You are billed metered GPU-seconds, never more than max_cost. "
                        "Egress and storage are shown separately and are not part of the GPU range.",
    }


async def create_unit_quote(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    kit: Any,
    expected_units: int,
    gpu_count: int,
    max_cost: decimal.Decimal,
    price_per_hour: decimal.Decimal | None,
    egress_gb: decimal.Decimal = D(0),
    storage_gb: decimal.Decimal = D(0),
    storage_days: int = 30,
) -> UnitQuote:
    if getattr(kit, "status", None) != "published":
        raise UnitQuoteError("kit_not_available", 409)
    if price_per_hour is None:
        price_per_hour = _dec(settings.UNIT_QUOTE_REFERENCE_PRICE_PER_HOUR, "0")
    if D(price_per_hour) <= 0:
        raise UnitQuoteError("no_price_available", 503)
    hints = (kit.manifest or {}).get("billing_hints") or {}
    unit = hints.get("unit") if isinstance(hints.get("unit"), str) else ""
    learned = await load_learned(db, kit.slug, kit.version, unit)
    body = compute_unit_quote(
        manifest=kit.manifest, expected_units=expected_units, gpu_count=gpu_count,
        price_per_hour=D(price_per_hour), fee_rate=await get_active_fee_rate(db), max_cost=max_cost,
        learned=learned, egress_gb=egress_gb, storage_gb=storage_gb, storage_days=storage_days,
    )
    quote = UnitQuote(
        customer_id=customer_id, kit_slug=kit.slug, kit_version=kit.version, unit=body["unit"],
        expected_units=expected_units, gpu_count=gpu_count, max_cost=_money(D(max_cost)), currency=CURRENCY,
        confidence=body["confidence"], capped=body["capped"], detail=body,
        expires_at=utcnow() + dt.timedelta(seconds=max(60, int(settings.UNIT_QUOTE_TTL_SECONDS))),
    )
    db.add(quote)
    await db.flush()
    return quote


async def link_quote_to_job(
    db: AsyncSession, *, quote_id: uuid.UUID, customer_id: uuid.UUID, job: Any
) -> UnitQuote:
    """Attach a live quote to the submitted job. The job's max_cost must not exceed the quote's cap."""
    from app.db.base import ensure_aware

    q = (
        await db.execute(select(UnitQuote).where(UnitQuote.id == quote_id, UnitQuote.customer_id == customer_id))
    ).scalar_one_or_none()
    if q is None:
        raise UnitQuoteError("quote_not_found", 404)
    if job.customer_id != customer_id:
        raise UnitQuoteError("job_not_found", 404)
    if q.job_id is not None and q.job_id != job.id:
        raise UnitQuoteError("quote_already_linked", 409)
    if ensure_aware(q.expires_at) <= utcnow() and q.job_id is None:
        raise UnitQuoteError("quote_expired", 409)
    if job.max_cost is None or D(job.max_cost) > D(q.max_cost):
        raise UnitQuoteError("job_cap_exceeds_quote_cap", 409)
    q.job_id = job.id
    await db.flush()
    return q


async def record_actual(
    db: AsyncSession, *, kit_slug: str, kit_version: str, unit: str, units: int,
    estimated_gpu_seconds: decimal.Decimal, actual_gpu_seconds: decimal.Decimal,
) -> UnitQuoteSample | None:
    """Store one (estimated, actual) pair for learning. Returns None when disabled or implausible."""
    if not enabled():
        return None
    est, act = D(estimated_gpu_seconds), D(actual_gpu_seconds)
    if units < 1 or est <= 0 or act <= 0 or not (SAMPLE_RATIO_SANE[0] <= act / est <= SAMPLE_RATIO_SANE[1]):
        return None
    s = UnitQuoteSample(
        kit_slug=kit_slug, kit_version=kit_version, unit=unit, units=units,
        estimated_gpu_seconds=est, actual_gpu_seconds=act,
    )
    db.add(s)
    await db.flush()
    return s


async def record_actual_for_job(db: AsyncSession, job: Any) -> UnitQuoteSample | None:
    """Completion hook: learn from a finished Kit job. Called inside ``safe_hook`` so a failure here never
    touches settlement. No-op unless PRICING_UNITS_ENABLED, the job is a Kit job and the agent reported a runtime."""
    if not enabled() or not getattr(job, "kit_id", None) or not getattr(job, "kit_version", None):
        return None
    from app.models.kits import Kit

    kit = (
        await db.execute(select(Kit).where(Kit.slug == job.kit_id, Kit.version == job.kit_version))
    ).scalar_one_or_none()
    hints = ((kit.manifest if kit is not None else None) or {}).get("billing_hints") or {}
    unit, per_unit = hints.get("unit"), _dec(hints.get("est_gpu_seconds_per_unit"))
    spec = ((job.spec_v2 or {}).get("spec") or {})
    units = int(((spec.get("billing") or {}).get("expected_units")) or 0)
    seconds = _dec((job.metrics or {}).get("duration_seconds"))
    if not isinstance(unit, str) or per_unit <= 0 or units < 1 or seconds <= 0:
        return None
    gpus = max(1, int(getattr(job, "gpu_count", 1) or 1))
    return await record_actual(
        db, kit_slug=job.kit_id, kit_version=job.kit_version, unit=unit, units=units,
        estimated_gpu_seconds=per_unit * units, actual_gpu_seconds=seconds * gpus,
    )
