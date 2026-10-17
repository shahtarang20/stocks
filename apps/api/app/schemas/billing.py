"""Billing request/response schemas (Phase 5).

Money is ALWAYS serialized out as a 2dp decimal STRING (never float). Amounts in
are decimal strings too, parsed to Decimal -> integer minor units at the edge.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from pydantic import BaseModel, Field, field_validator

CURRENCY = "USDC_CREDITS"


class AmountIn(BaseModel):
    amount: decimal.Decimal = Field(gt=0)

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce(cls, v):
        return decimal.Decimal(str(v))


class AccountOut(BaseModel):
    currency: str = CURRENCY
    available: str
    reserved: str
    balance: str


class LedgerEntryOut(BaseModel):
    entry_id: uuid.UUID
    transaction_id: uuid.UUID
    kind: str
    direction: str
    amount: str
    currency: str = CURRENCY
    reference_type: str | None = None
    reference_id: str | None = None
    created_at: dt.datetime


class LedgerPageOut(BaseModel):
    items: list[LedgerEntryOut]
    page: int
    page_size: int
    total: int


class LineItem(BaseModel):
    label: str
    amount: str


class InvoiceOut(BaseModel):
    invoice_id: uuid.UUID
    job_id: uuid.UUID
    line_items: list[LineItem]
    estimated_total: str
    actual_total: str
    platform_fee: str
    currency: str = CURRENCY
    status: str
    created_at: dt.datetime


class UsageRecordOut(BaseModel):
    job_id: uuid.UUID
    estimated_minutes: int
    actual_seconds: int
    estimated_cost: str
    actual_cost: str
    currency: str = CURRENCY
    created_at: dt.datetime


class UsageSummaryOut(BaseModel):
    currency: str = CURRENCY
    total_estimated: str
    total_actual: str
    records: list[UsageRecordOut]


# ---------------------------------------------------------------------------
# Disputes
# ---------------------------------------------------------------------------
class DisputeCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=1024)


class DisputeOut(BaseModel):
    dispute_id: uuid.UUID
    job_id: uuid.UUID
    reason: str
    status: str
    resolution: str | None = None
    refund_amount: str | None = None
    created_at: dt.datetime
    resolved_at: dt.datetime | None = None


# ---------------------------------------------------------------------------
# Provider earnings + payouts
# ---------------------------------------------------------------------------
class EarningsOut(BaseModel):
    currency: str = CURRENCY
    available: str
    lifetime: str
    recent: list[LedgerEntryOut]


class PayoutOut(BaseModel):
    payout_id: uuid.UUID
    amount: str
    currency: str = CURRENCY
    status: str
    created_at: dt.datetime
    settled_at: dt.datetime | None = None


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
class RefundCreate(BaseModel):
    job_id: uuid.UUID | None = None
    invoice_id: uuid.UUID | None = None
    amount: decimal.Decimal = Field(gt=0)
    reason: str = Field(min_length=1, max_length=512)

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce(cls, v):
        return decimal.Decimal(str(v))


class RefundOut(BaseModel):
    refund_id: uuid.UUID
    job_id: uuid.UUID | None
    amount: str
    currency: str = CURRENCY
    reason: str | None = None
    created_at: dt.datetime


class DisputeResolve(BaseModel):
    resolution: str = Field(min_length=1, max_length=1024)
    refund_amount: decimal.Decimal | None = None

    @field_validator("refund_amount", mode="before")
    @classmethod
    def _coerce(cls, v):
        return None if v is None else decimal.Decimal(str(v))


class ReconciliationAccountOut(BaseModel):
    account_id: uuid.UUID
    owner_type: str
    type: str
    balance: str


class ReconciliationOut(BaseModel):
    balanced: bool
    total_debits: str
    total_credits: str
    imbalance: str
    currency: str = CURRENCY
    accounts: list[ReconciliationAccountOut]


# ---------------------------------------------------------------------------
# Stripe Checkout / Payments (Workstream C)
# ---------------------------------------------------------------------------
class CheckoutSessionCreate(BaseModel):
    amount: decimal.Decimal = Field(gt=0, description="Amount in USD to purchase credits for")
    success_url: str | None = None
    cancel_url: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce(cls, v):
        return decimal.Decimal(str(v))


class CheckoutSessionOut(BaseModel):
    session_id: str
    url: str
    amount: str
    currency: str = "usd"


class StripeWebhookOut(BaseModel):
    received: bool = True
    event_id: str
    event_type: str | None = None
    status: str
    transaction_id: str | None = None

