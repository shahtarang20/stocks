"""Marketplace models (Phase 3): pricing rules, quotes, resource reservations.

All column types are portable (Uuid, JSON, Numeric, DateTime(timezone=True),
String/Integer/Boolean) so the models run on SQLite (tests) and Postgres (prod).
Money is always ``Numeric`` and serialized to a 2dp decimal string at the edge.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow

CURRENCY = "USDC_CREDITS"

# Status vocabularies (checked varchar — portable Enum).
QUOTE_STATUSES = ("active", "expired", "reserved", "cancelled")
RESERVATION_STATUSES = ("active", "released", "expired", "consumed")


class PricingRule(Base, TimestampMixin):
    __tablename__ = "pricing_rules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    platform_fee_rate: Mapped[decimal.Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, default=decimal.Decimal("0.10")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)


class Quote(Base, TimestampMixin):
    __tablename__ = "quotes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    machine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("provider_machines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workload_type: Mapped[str] = mapped_column(String(64), nullable=False)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    # Snapshotted pricing — stable for the life of the quote.
    price_per_hour_snapshot: Mapped[decimal.Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    platform_fee_rate: Mapped[decimal.Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    gpu_cost: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    platform_fee: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    total: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(32), nullable=False, default=CURRENCY)

    max_authorized_spend: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    reservation: Mapped["ResourceReservation | None"] = relationship(
        back_populates="quote", uselist=False
    )


class ResourceReservation(Base, TimestampMixin):
    __tablename__ = "resource_reservations"
    __table_args__ = (
        UniqueConstraint("quote_id", name="uq_resource_reservations_quote_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    quote_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False
    )
    machine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("provider_machines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slots: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    quote: Mapped["Quote"] = relationship(back_populates="reservation")
