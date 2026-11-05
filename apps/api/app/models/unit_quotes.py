"""Unit quotes (U15). Display and estimate only; billing stays metered GPU-seconds (P5)."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class UnitQuote(Base, TimestampMixin):
    __tablename__ = "unit_quotes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kit_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    kit_version: Mapped[str] = mapped_column(String(32), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_cost: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    capped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UnitQuoteSample(Base, TimestampMixin):
    __tablename__ = "unit_quote_samples"
    __table_args__ = (Index("ix_unit_quote_samples_kit", "kit_slug", "kit_version", "unit"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kit_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    kit_version: Mapped[str] = mapped_column(String(32), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    estimated_gpu_seconds: Mapped[decimal.Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    actual_gpu_seconds: Mapped[decimal.Decimal] = mapped_column(Numeric(18, 4), nullable=False)
