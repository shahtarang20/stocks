"""Billing / double-entry ledger models (Phase 5 — the money layer).

Money is stored as **integer minor units** (``BigInteger``) — NEVER float. Every
account carries a ``currency`` + fixed exponent (2). The ledger is double-entry:
each :class:`LedgerTransaction` has >= 2 :class:`LedgerEntry` rows whose debits and
credits sum equal (INVARIANT 1), enforced by the posting choke point
(``app.services.ledger.post_transaction``).

Portable column types only (Uuid, BigInteger, JSON, String, DateTime(timezone=True))
so the models run on SQLite (tests) and Postgres (prod).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, utcnow

CURRENCY = "USDC_CREDITS"

# Account types + their normal side. CREDIT-normal: balance = credits - debits.
# DEBIT-normal: balance = debits - credits (used only for the external funding
# account, which tracks net funds injected into the system).
ACCOUNT_TYPES = (
    "customer_credits",
    "provider_earnings",
    "platform_revenue",
    "external",
    "holding",
    "payout_pending",
)
OWNER_TYPES = ("customer", "provider", "platform", "external", "holding")

# Transaction kinds (contract §Double-entry ledger).
TXN_KINDS = (
    "credit_purchase",
    "hold",
    "capture",
    "release",
    "payout_request",
    "payout_settle",
    "refund",
    "adjustment",
)

DIRECTIONS = ("debit", "credit")


class BillingAccount(Base):
    """A ledger account. ``balance_minor`` is a cached balance kept in sync inside
    the same DB transaction as the entries (see post_transaction)."""

    __tablename__ = "billing_accounts"
    __table_args__ = (
        UniqueConstraint(
            "owner_type", "owner_id", "type", name="uq_billing_accounts_owner_type"
        ),
        Index("ix_billing_accounts_type", "type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_type: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(32), nullable=False, default=CURRENCY)
    balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class LedgerTransaction(Base):
    """A balanced posting. ``idempotency_key`` is UNIQUE — a repeat returns the
    existing transaction with NO second effect (how double-charge is prevented)."""

    __tablename__ = "ledger_transactions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ledger_transactions_idem"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="posted")
    reference_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan"
    )


class LedgerEntry(Base):
    """An immutable, append-only entry. ``amount_minor`` is always > 0; the sign is
    carried by ``direction`` (debit|credit)."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        Index("ix_ledger_entries_account_id", "account_id"),
        Index("ix_ledger_entries_transaction_id", "transaction_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("ledger_transactions.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("billing_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(32), nullable=False, default=CURRENCY)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    transaction: Mapped["LedgerTransaction"] = relationship(back_populates="entries")


class UsageRecord(Base):
    """Metered usage per settled job — estimated vs actual kept separate."""

    __tablename__ = "usage_records"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    estimated_minutes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    actual_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    estimated_cost_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    actual_cost_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(32), nullable=False, default=CURRENCY)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Invoice(Base):
    """An itemized invoice per settled job."""

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_invoices_job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    estimated_total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    actual_total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    platform_fee_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(32), nullable=False, default=CURRENCY)
    # paid | not_charged (failed job) | refunded
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="paid")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Payment(Base):
    """A credit top-up (credit_purchase via simulated or Stripe)."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(32), nullable=False, default=CURRENCY)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="succeeded")
    provider_configuration: Mapped[str] = mapped_column(String(32), nullable=False, default="simulated")
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("ledger_transactions.id", ondelete="SET NULL"), nullable=True
    )
    external_reference: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    stripe_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Refund(Base):
    """An authorized, audited refund that reverses (part of) a charge."""

    __tablename__ = "refunds"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(32), nullable=False, default=CURRENCY)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("ledger_transactions.id", ondelete="SET NULL"), nullable=True
    )
    stripe_refund_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ProviderPayout(Base):
    """A provider payout request (and its settlement)."""

    __tablename__ = "provider_payouts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(32), nullable=False, default=CURRENCY)
    # requested | settled | cancelled
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="requested")
    request_txn_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("ledger_transactions.id", ondelete="SET NULL"), nullable=True
    )
    settle_txn_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("ledger_transactions.id", ondelete="SET NULL"), nullable=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    settled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Dispute(Base):
    """A customer dispute opened on a settled job."""

    __tablename__ = "disputes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(String(1024), nullable=False)
    # open | resolved | rejected
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    resolution: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    refund_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
