"""Provider reputation, slashing and dispute-evidence models (Phase B).

WHY an append-only ``reputation_events`` log next to a mutable
``provider_reputation`` aggregate: the aggregate is what the scheduler reads on
the hot path (one indexed row per provider), while the log is what an auditor or
a disputing provider reads to answer "why is my score 0.31?". The aggregate can
always be rebuilt from the log.

Money note: a slash is a ledger movement like any other — ``ledger_txn_id``
points at the balanced transaction posted through
``app.services.ledger.post_transaction``. This table never holds balances.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, utcnow

# Checked-varchar vocabularies.
REPUTATION_EVENT_KINDS = (
    "job_completed",
    "job_failed",
    # A provider that let a lease lapse mid-job: distinct from job_failed,
    # which is an honest failure report. Written by app/services/failover.py.
    "lease_expired",
    "verification_passed",
    "verification_failed",
    "redundancy_mismatch",
    "dispute_lost",
    "dispute_won",
    "slash",
    "decay",
    "manual_adjustment",
)
DISPUTE_EVIDENCE_KINDS = ("input", "output", "hash", "challenge", "redundancy", "note")

# Neutral starting score — matches the scheduler's default for an unknown
# machine (app/services/scheduler.py), so a brand-new provider is neither
# punished nor preferred.
NEUTRAL_SCORE = decimal.Decimal("0.5000")


class ProviderReputation(Base):
    """Rolling reputation aggregate for a provider (optionally per machine).

    ``provider_id`` is UNIQUE: the provider-level row is the authoritative one.
    ``machine_id`` is informational for provider-level rows and records which
    machine most recently moved the score.
    """

    __tablename__ = "provider_reputation"
    __table_args__ = (
        UniqueConstraint("provider_id", name="uq_provider_reputation_provider_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False
    )
    machine_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("provider_machines.id", ondelete="SET NULL"), nullable=True, index=True
    )
    score: Mapped[decimal.Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, default=NEUTRAL_SCORE
    )
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verification_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disputes_lost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Last time time-decay was applied; used so decay is idempotent per window.
    decayed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ReputationEvent(Base):
    """Append-only reputation ledger. Never updated, never deleted."""

    __tablename__ = "reputation_events"
    __table_args__ = (
        Index("ix_reputation_events_provider_created", "provider_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    machine_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("provider_machines.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # Signed change applied to the aggregate score (may be 0 for pure audit rows).
    delta: Mapped[decimal.Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, default=decimal.Decimal("0")
    )
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SlashEvent(Base):
    """A punitive deduction from a provider's earnings.

    ``amount_minor`` is integer minor units (never float). ``ledger_txn_id`` is
    the balanced transaction that actually moved the money; a row with a NULL
    txn id means "recorded but not yet posted" and must never be treated as a
    completed slash.
    """

    __tablename__ = "slash_events"
    __table_args__ = (
        Index("ix_slash_events_provider_created", "provider_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(32), nullable=False, default="USDC_CREDITS")
    reason: Mapped[str] = mapped_column(String(1024), nullable=False)
    ledger_txn_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("ledger_transactions.id", ondelete="SET NULL"), nullable=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class DisputeEvidence(Base):
    """A single piece of evidence attached to a dispute.

    Kept append-only so neither side can quietly rewrite the record after an
    adjudication; ``payload`` holds hashes/pointers, never raw artifact bytes.
    """

    __tablename__ = "dispute_evidence"
    __table_args__ = (
        Index("ix_dispute_evidence_dispute_kind", "dispute_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    dispute_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # input | output | hash | challenge | redundancy | note
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
