"""Batch jobs, datasets and training checkpoints (Phase C).

WHY ``batches.idempotency_key`` is UNIQUE per customer (and NULL is allowed):
the same retry semantics as ``jobs`` — a client may replay a batch submission
safely, while an unkeyed submission is never implicitly deduped.

Dataset/checkpoint rows are metadata only: bytes live in object storage under
``object_key`` and are content-addressed by ``sha256``.
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
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, utcnow

BATCH_STATUSES = ("created", "running", "completed", "failed", "cancelled")
DATASET_STATUSES = ("pending", "uploaded", "failed", "deleted")


class Batch(Base):
    """A customer-submitted group of jobs priced with a bulk discount."""

    __tablename__ = "batches"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "idempotency_key", name="uq_batches_customer_idempotency_key"
        ),
        Index("ix_batches_customer_status", "customer_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # created | running | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Disclosed discount applied to every job in the batch. Decimal, never float.
    discount_pct: Mapped[decimal.Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=decimal.Decimal("0")
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Dataset(Base):
    """A customer-uploaded training/inference dataset stored in object storage."""

    __tablename__ = "datasets"
    __table_args__ = (
        Index("ix_datasets_customer_status", "customer_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_type: Mapped[str] = mapped_column(
        String(128), nullable=False, default="application/octet-stream"
    )
    # pending | uploaded | failed | deleted
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="uploaded")
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class TrainingCheckpoint(Base):
    """A per-step checkpoint emitted by a training job (resumable fine-tuning)."""

    __tablename__ = "training_checkpoints"
    __table_args__ = (
        UniqueConstraint("job_id", "step", name="uq_training_checkpoints_job_step"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
