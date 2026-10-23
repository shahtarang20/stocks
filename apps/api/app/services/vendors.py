"""Vendor quota status and exactly-once alerts (usage thresholds + renewals).

Two independent alarms per service:

* **usage threshold** — "you are at 80% of Resend's monthly emails". Needs a
  quota AND a usage reading; silent when either is missing, because a bar drawn
  from a number we do not have is worse than no bar.
* **renewal due** — "Railway renews in 3 days". Needs only a date a human
  entered, so it is the one alarm that works with no vendor integration at all.

Both are recorded in ``vendor_alerts`` and de-duplicated by a UNIQUE
constraint rather than by application logic, so two concurrent sweeps cannot
double-fire.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.vendors import (
    ALERT_RENEWAL_DUE,
    ALERT_USAGE_THRESHOLD,
    NO_THRESHOLD,
    VendorAlert,
    VendorService,
)

DEFAULT_THRESHOLDS = (50, 80, 100)

#: Ordered worst-first; the UI colours off these.
STATUS_CRITICAL = "critical"
STATUS_WARNING = "warning"
STATUS_WATCH = "watch"
STATUS_OK = "ok"
#: Not a failure — an honest "we cannot measure this one".
STATUS_UNKNOWN = "unknown"


def _thresholds(vendor: VendorService) -> list[int]:
    raw = (vendor.alert_thresholds or "").strip()
    if not raw:
        return list(DEFAULT_THRESHOLDS)
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return sorted(set(out)) or list(DEFAULT_THRESHOLDS)


def period_key(vendor: VendorService, now: dt.datetime | None = None) -> str:
    """The window a usage alert belongs to, so next window fires again."""
    now = now or utcnow()
    if vendor.quota_period == "day":
        return now.date().isoformat()
    if vendor.quota_period == "none":
        return "all-time"
    return f"{now.year:04d}-{now.month:02d}"


@dataclass(frozen=True)
class VendorStatus:
    """Everything one card needs, with the provenance of each number."""

    usage_pct: float | None
    status: str
    #: Why the status is what it is, in words the admin can act on.
    detail: str
    days_until_renewal: int | None
    renewal_due: bool
    measurable: bool


def evaluate(vendor: VendorService, now: dt.datetime | None = None) -> VendorStatus:
    """Derive a card's status WITHOUT inventing anything it does not know."""
    now = now or utcnow()
    today = now.date()

    days_until: int | None = None
    renewal_due = False
    if vendor.renewal_date is not None:
        days_until = (vendor.renewal_date - today).days
        renewal_due = days_until <= (vendor.reminder_days_before or 0)

    limit = vendor.quota_limit
    usage = vendor.current_usage

    # No ceiling to be a percentage of. Railway and Stripe live here: Railway
    # bills by compute-seconds and Stripe per transaction, so "% of quota" would
    # be a fabricated figure.
    if limit is None or limit <= 0:
        detail = (
            "Usage-billed: no fixed ceiling, so there is no percentage to show. "
            "Watch the cost, not a bar."
        )
        status = STATUS_OK
        if renewal_due and days_until is not None:
            status = STATUS_WARNING
            detail = _renewal_detail(days_until)
        return VendorStatus(None, status, detail, days_until, renewal_due, False)

    if usage is None or vendor.usage_source == "unavailable":
        detail = (
            f"No usage reading. {vendor.name} exposes no usage API, so this "
            "cannot be measured automatically — check the dashboard or set it "
            "manually."
        )
        status = STATUS_UNKNOWN
        if renewal_due and days_until is not None:
            detail = _renewal_detail(days_until) + " " + detail
        return VendorStatus(None, status, detail, days_until, renewal_due, False)

    pct = float(decimal.Decimal(usage) / decimal.Decimal(limit) * 100)
    if pct >= 100:
        status, detail = STATUS_CRITICAL, (
            f"Over the {vendor.plan_name or 'plan'} limit "
            f"({_fmt(usage)}/{_fmt(limit)} {vendor.quota_unit or ''}). "
            "Expect throttling or charges."
        )
    elif pct >= 80:
        status, detail = STATUS_WARNING, (
            f"At {pct:.0f}% of the limit ({_fmt(usage)}/{_fmt(limit)} "
            f"{vendor.quota_unit or ''})."
        )
    elif pct >= 50:
        status, detail = STATUS_WATCH, f"At {pct:.0f}% of the limit."
    else:
        status, detail = STATUS_OK, f"At {pct:.0f}% of the limit."

    if vendor.usage_source == "measured":
        detail += " Counted by us, not read from the vendor — treat as an estimate."

    if renewal_due and days_until is not None:
        detail = _renewal_detail(days_until) + " " + detail
        if status in (STATUS_OK, STATUS_WATCH):
            status = STATUS_WARNING

    return VendorStatus(pct, status, detail, days_until, renewal_due, True)


def _renewal_detail(days_until: int) -> str:
    if days_until < 0:
        return f"Renewal date passed {abs(days_until)} day(s) ago."
    if days_until == 0:
        return "Renews TODAY."
    return f"Renews in {days_until} day(s)."


def _fmt(v: decimal.Decimal | None) -> str:
    if v is None:
        return "?"
    d = decimal.Decimal(v)
    return str(int(d)) if d == d.to_integral_value() else f"{d:.2f}"


async def _fire(
    db: AsyncSession,
    vendor: VendorService,
    *,
    alert_type: str,
    threshold_pct: int,
    key: str,
    message: str,
) -> VendorAlert | None:
    """Insert one alert, or return None if it already fired for this period.

    The UNIQUE constraint is the de-duplicator. A SAVEPOINT keeps the losing
    insert from poisoning the caller's transaction.
    """
    alert = VendorAlert(
        vendor_id=vendor.id,
        alert_type=alert_type,
        threshold_pct=threshold_pct,
        period_key=key,
        message=message,
    )
    try:
        async with db.begin_nested():
            db.add(alert)
            await db.flush()
    except IntegrityError:
        return None
    return alert


async def evaluate_and_alert(
    db: AsyncSession, *, now: dt.datetime | None = None
) -> list[VendorAlert]:
    """Sweep every active service, firing anything newly crossed or due."""
    now = now or utcnow()
    vendors = (
        await db.scalars(select(VendorService).where(VendorService.active.is_(True)))
    ).all()

    fired: list[VendorAlert] = []
    for vendor in vendors:
        st = evaluate(vendor, now)

        if st.usage_pct is not None:
            for threshold in _thresholds(vendor):
                if st.usage_pct >= threshold:
                    a = await _fire(
                        db,
                        vendor,
                        alert_type=ALERT_USAGE_THRESHOLD,
                        threshold_pct=threshold,
                        key=period_key(vendor, now),
                        message=(
                            f"{vendor.name} reached {threshold}% of its "
                            f"{vendor.quota_unit or 'quota'} limit "
                            f"({_fmt(vendor.current_usage)}/"
                            f"{_fmt(vendor.quota_limit)}). Source: "
                            f"{vendor.usage_source}."
                        ),
                    )
                    if a is not None:
                        fired.append(a)

        if vendor.renewal_date is not None and st.renewal_due:
            a = await _fire(
                db,
                vendor,
                alert_type=ALERT_RENEWAL_DUE,
                threshold_pct=NO_THRESHOLD,
                # Keyed by the date itself, so moving the renewal forward a
                # month is a NEW reminder rather than a suppressed duplicate.
                key=vendor.renewal_date.isoformat(),
                message=(
                    f"{vendor.name} subscription "
                    f"({vendor.plan_name or 'plan'}"
                    + (
                        f", {vendor.cost_currency} {_fmt(vendor.cost_amount)}"
                        if vendor.cost_amount is not None
                        else ""
                    )
                    + f") renews on {vendor.renewal_date.isoformat()} — "
                    f"{_renewal_detail(st.days_until_renewal or 0)}"
                ),
            )
            if a is not None:
                fired.append(a)

    return fired


async def record_usage(
    db: AsyncSession,
    key: str,
    value: decimal.Decimal | int | float,
    *,
    source: str = "measured",
    now: dt.datetime | None = None,
) -> VendorService | None:
    """Set a service's current usage. Returns None if the key is unknown."""
    vendor = await db.scalar(select(VendorService).where(VendorService.key == key))
    if vendor is None:
        return None
    vendor.current_usage = decimal.Decimal(str(value))
    vendor.usage_source = source
    vendor.usage_updated_at = now or utcnow()
    return vendor


async def get_vendor(db: AsyncSession, vendor_id: uuid.UUID) -> VendorService | None:
    return await db.scalar(select(VendorService).where(VendorService.id == vendor_id))
