"""Verification models (Phase B): known-answer challenges, verification results
and redundant (shadow) execution checks.

WHY these live in their own tables rather than inside ``jobs.verification`` JSON:
verification outcomes drive money (payout holds, slashing) and disputes, so they
must be queryable, append-only and auditable — a JSON blob on the job row can be
overwritten by the next status update and cannot be indexed or joined.

Portable column types only (Uuid, JSON, Numeric, Boolean, String,
DateTime(timezone=True)) so the models run on SQLite (tests) and Postgres (prod).
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, utcnow

# Checked-varchar vocabularies (portable Enum — see app/models/model_registry.py).
VERIFICATION_KINDS = ("challenge", "redundant", "model_identity")
REDUNDANCY_STATUSES = ("pending", "running", "matched", "mismatched", "inconclusive")


class VerificationChallenge(Base):
    """A known-answer challenge: a canned input whose correct output hash we know.

    The scheduler injects these as ordinary-looking jobs (``jobs.is_challenge``)
    at ``CHALLENGE_SAMPLE_RATE`` so a provider cannot tell a challenge apart from
    paid work. ``expected_output_hash`` is the sha256 of the canonical output.
    """

    __tablename__ = "verification_challenges"
    __table_args__ = (
        Index("ix_verification_challenges_workload_active", "workload_type", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workload_type: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # The job ``input`` payload submitted verbatim when this challenge is used.
    input: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    expected_output_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Comparison policy, e.g. {"mode": "exact"} or {"mode": "cosine", "min": 0.98}.
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class VerificationResult(Base):
    """One verification attempt against one job. Append-only evidence."""

    __tablename__ = "verification_results"
    __table_args__ = (
        Index("ix_verification_results_job_kind", "job_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    challenge_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("verification_challenges.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # challenge | redundant | model_identity
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expected_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class RedundancyCheck(Base):
    """Links a primary job to its shadow re-execution on a different machine.

    ``matched``/``similarity`` stay NULL until the shadow finishes, so a pending
    row is unambiguous (NULL != False) and the resolver is idempotent on
    ``status``.
    """

    __tablename__ = "redundancy_checks"
    __table_args__ = (
        Index("ix_redundancy_checks_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    primary_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shadow_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # pending | running | matched | mismatched | inconclusive
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    matched: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    similarity: Mapped[decimal.Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
