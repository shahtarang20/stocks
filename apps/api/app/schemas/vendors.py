"""Vendor service schemas. Money is a 2dp string, never a float."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from pydantic import BaseModel, ConfigDict, Field


def _money(v: decimal.Decimal | None) -> str | None:
    if v is None:
        return None
    return f"{decimal.Decimal(v).quantize(decimal.Decimal('0.01'))}"


def _num(v: decimal.Decimal | None) -> str | None:
    if v is None:
        return None
    d = decimal.Decimal(v)
    return str(int(d)) if d == d.to_integral_value() else f"{d:.2f}"


class VendorCardOut(BaseModel):
    """One admin card. Every number carries where it came from."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    name: str
    category: str
    purpose: str
    dashboard_url: str | None = None

    plan_name: str | None = None
    quota_limit: str | None = None
    quota_unit: str | None = None
    quota_period: str
    current_usage: str | None = None
    #: "measured" | "vendor_api" | "manual" | "unavailable" — shown on the card
    #: so an estimate is never mistaken for the vendor's invoice.
    usage_source: str
    usage_updated_at: dt.datetime | None = None

    cost_amount: str | None = None
    cost_currency: str
    billing_cycle: str
    renewal_date: dt.date | None = None
    reminder_days_before: int

    # Derived
    usage_pct: float | None = None
    status: str
    detail: str
    days_until_renewal: int | None = None
    renewal_due: bool = False
    measurable: bool = False

    notes: str | None = None
    active: bool


class VendorUpdate(BaseModel):
    """Everything an administrator may set. Usage is not editable here —
    it is either measured or explicitly pushed via the usage endpoint."""

    plan_name: str | None = Field(default=None, max_length=64)
    quota_limit: decimal.Decimal | None = Field(default=None, ge=0)
    quota_unit: str | None = Field(default=None, max_length=32)
    quota_period: str | None = Field(default=None, pattern="^(day|month|none)$")
    cost_amount: decimal.Decimal | None = Field(default=None, ge=0)
    cost_currency: str | None = Field(default=None, max_length=8)
    billing_cycle: str | None = Field(
        default=None, pattern="^(monthly|annual|usage|per_transaction|free)$"
    )
    renewal_date: dt.date | None = None
    reminder_days_before: int | None = Field(default=None, ge=0, le=90)
    alert_thresholds: str | None = Field(default=None, max_length=64)
    notes: str | None = None
    active: bool | None = None


class VendorUsageIn(BaseModel):
    value: decimal.Decimal = Field(ge=0)
    source: str = Field(default="manual", pattern="^(measured|vendor_api|manual)$")


class VendorAlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    vendor_id: uuid.UUID
    vendor_name: str | None = None
    alert_type: str
    threshold_pct: int | None = None
    period_key: str
    message: str
    acknowledged: bool
    created_at: dt.datetime


class VendorSummaryOut(BaseModel):
    """Headline counts for the top of the page."""

    total: int
    critical: int
    warning: int
    unknown: int
    renewals_due: int
    unacknowledged_alerts: int
    #: Sum of configured costs only. Services with no cost set are EXCLUDED and
    #: counted separately, so this is never passed off as a complete bill.
    known_monthly_cost: str
    cost_currency: str
    services_without_cost: int
