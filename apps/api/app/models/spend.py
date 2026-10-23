"""Spend limits and threshold alerts (Phase B).

WHY ``spend_alerts`` exists as a table with a UNIQUE
``(spend_limit_id, threshold_pct, period_key)``: alerting runs on every spend
event, so the DB constraint — not application bookkeeping — is what guarantees a
customer gets the "80% of your monthly budget" email exactly once per period.
Insert-first-then-send makes the send idempotent under concurrency and retries.

Money is integer minor units (``BigInteger``) — never float.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, utcnow

SPEND_SCOPES = ("customer", "project", "api_key")
SPEND_PERIODS = ("daily", "monthly", "total")


class SpendLimit(Base):
    """A budget cap for one scope + period.

    ``scope_id`` is a loose Uuid rather than a typed FK because the referenced
    table varies with ``scope`` (users / projects / api_keys); the owning
    service resolves and authorizes it.
    """

    __tablename__ = "spend_limits"
    __table_args__ = (
        UniqueConstraint(
            "scope", "scope_id", "period", name="uq_spend_limits_scope_scope_id_period"
        ),
        Index("ix_spend_limits_scope_scope_id", "scope", "scope_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # customer | project | api_key
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # The user who owns this limit — carried explicitly so an ownership filter
    # (the IDOR rule) never needs a polymorphic join.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # daily | monthly | total
    period: Mapped[str] = mapped_column(String(32), nullable=False)
    limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(32), nullable=False, default="USDC_CREDITS")
    # Percentages at which to alert, e.g. [50, 80, 100].
    alert_thresholds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # A breached limit may warn only, or hard-block new spend.
    enforce: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class SpendAlert(Base):
    """One fired threshold alert. The UNIQUE constraint IS the idempotency key."""

    __tablename__ = "spend_alerts"
    __table_args__ = (
        UniqueConstraint(
            "spend_limit_id",
            "threshold_pct",
            "period_key",
            name="uq_spend_alerts_limit_threshold_period",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    spend_limit_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("spend_limits.id", ondelete="CASCADE"), nullable=False, index=True
    )
    threshold_pct: Mapped[int] = mapped_column(Integer, nullable=False)
    # Bucket identifier for the period, e.g. "2026-10" (monthly), "2026-10-23"
    # (daily) or "total" — resets alerting when the window rolls over.
    period_key: Mapped[str] = mapped_column(String(32), nullable=False)
    spent_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sent_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
