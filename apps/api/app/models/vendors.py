"""Third-party vendor services: quotas, costs and renewal reminders.

WHY this exists: ComputeCoin runs on other people's infrastructure — Railway,
Vercel, Supabase, Upstash, Resend, Sentry, an RPC provider and Stripe. Each has
its own free-tier ceiling and its own billing date, and blowing through one
degrades or stops the platform with no warning from inside the product.

HONESTY RULE for this module: a usage number is only ever as good as its
source, and the source is recorded on every row.

* ``measured``    — counted by us at the call site. Real, but it is OUR count,
                    not the vendor's invoice. Retries, other apps sharing the
                    same key, and the vendor's own counting rules all cause
                    drift.
* ``vendor_api``  — read from the vendor's own API. Authoritative.
* ``manual``      — a number a human typed in.
* ``unavailable`` — the vendor exposes no usage endpoint (Resend, today). The
                    usage stays NULL and the UI must say so.

``usage_source`` must be surfaced wherever a number is. A measured estimate
displayed as if it were the vendor's billing truth is exactly the "demo data
presented as live infrastructure" failure the project forbids — the dashboard
would read green while a real invoice lands.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow

#: Where a usage figure came from. Never defaults to something flattering.
USAGE_SOURCES = ("measured", "vendor_api", "manual", "unavailable")

#: How the vendor charges. ``usage`` means there is no hard ceiling to be at a
#: percentage OF — Railway bills by compute-seconds, so a "% used" bar would be
#: a fiction. Those rows carry a cost estimate instead.
BILLING_CYCLES = ("monthly", "annual", "usage", "per_transaction", "free")

#: Sentinel for "this alert is not a percentage threshold". See VendorAlert.
NO_THRESHOLD = -1

ALERT_USAGE_THRESHOLD = "usage_threshold"
ALERT_RENEWAL_DUE = "renewal_due"


class VendorService(Base):
    """One third-party dependency, its ceiling, its cost and its renewal date."""

    __tablename__ = "vendor_services"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    #: Stable slug used by the metering call sites (``resend``, ``upstash`` ...).
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(255), nullable=False)
    dashboard_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    plan_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: NULL means "no hard ceiling" (usage-billed). A percentage is only
    #: meaningful when this is set.
    quota_limit: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(20, 4), nullable=True
    )
    quota_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: "day" | "month" | "none" — scopes the period key alerts are keyed by.
    quota_period: Mapped[str] = mapped_column(String(16), nullable=False, default="month")

    current_usage: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(20, 4), nullable=True
    )
    usage_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unavailable"
    )
    usage_updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Money. Left NULL until a human fills it in — inventing a cost would be
    #: fabricating financial data.
    cost_amount: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    cost_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    billing_cycle: Mapped[str] = mapped_column(String(16), nullable=False, default="free")

    #: The subscription renewal / next-invoice date. NULL until set, and the UI
    #: says "not set" rather than guessing.
    renewal_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    reminder_days_before: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7
    )

    alert_thresholds: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class VendorAlert(Base):
    """An alert that has fired, recorded so it fires EXACTLY once.

    Same shape as the customer spend alerts: the UNIQUE constraint — not
    application logic — is what makes "once per period" true under concurrency.
    ``period_key`` is the billing window ("2026-10" / "2026-10-23") for a usage
    threshold, or the renewal date for a renewal reminder, so next period's
    crossing is a different row and fires again.
    """

    __tablename__ = "vendor_alerts"
    __table_args__ = (
        UniqueConstraint(
            "vendor_id",
            "alert_type",
            "threshold_pct",
            "period_key",
            name="uq_vendor_alert_once",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    vendor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: ``NO_THRESHOLD`` (-1) for renewal reminders, which are not a percentage
    #: of anything. Deliberately NOT NULL: SQL treats NULLs as DISTINCT, so a
    #: NULL here would silently defeat the UNIQUE constraint and every sweep
    #: would insert another copy of the same reminder.
    threshold_pct: Mapped[int] = mapped_column(
        Integer, nullable=False, default=NO_THRESHOLD
    )
    period_key: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
