"""Outbound webhook endpoints and their delivery attempts (Phase B).

SSRF note: ``url`` is attacker-controlled by definition (a customer types it),
so the *delivery* path must validate it against the private-network blocklist
before every request — see the webhook service. Storing a URL is not trusting it.

Secrets: only ``secret_hash`` is persisted; the raw signing secret is shown once
at creation, mirroring ``api_keys.key_hash`` and ``enrollment_tokens.token_hash``.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base, utcnow

WEBHOOK_DELIVERY_STATUSES = ("pending", "delivered", "failed")


class WebhookEndpoint(Base):
    """A customer-registered HTTPS sink for platform events."""

    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        Index("ix_webhook_endpoints_customer_active", "customer_id", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    secret_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # Subscribed event types, e.g. ["job.completed", "job.failed"].
    events: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Consecutive failures; the dispatcher auto-disables a chronically dead sink.
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    deliveries: Mapped[list["WebhookDelivery"]] = relationship(
        back_populates="endpoint", cascade="all, delete-orphan"
    )


class WebhookDelivery(Base):
    """One queued/attempted delivery.

    ``delivery_key`` is globally UNIQUE and derived from (endpoint, event type,
    source object id) so a replayed source event enqueues at most one delivery —
    the idempotency rule for externally-triggered mutations.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("delivery_key", name="uq_webhook_deliveries_delivery_key"),
        Index("ix_webhook_deliveries_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    delivery_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # pending | delivered | failed
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    delivered_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    endpoint: Mapped["WebhookEndpoint"] = relationship(back_populates="deliveries")
