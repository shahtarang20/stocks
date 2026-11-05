"""Interactive sessions (U10, docs/architecture/U10-interactive-sessions-design.md).

Only token HASHES are stored. A host or browser token is single use: ``*_used_at`` is set by an atomic
conditional UPDATE when the relay socket is accepted. Public keys are X25519 (hex); the relay never holds a
private key or a derived session key.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

SESSION_PENDING = "pending"  # created and funded, waiting for a host to claim it
SESSION_STARTING = "starting"  # claimed; the host is starting the container (not billed)
SESSION_ACTIVE = "active"  # the host reported the container running (billed from started_at)
SESSION_STOPPED = "stopped"
SESSION_FAILED = "failed"
SESSION_LIVE = (SESSION_PENDING, SESSION_STARTING, SESSION_ACTIVE)

# stop_reason
STOP_USER = "user"
STOP_IDLE = "idle_timeout"
STOP_MAX_DURATION = "max_duration"
STOP_HOST_LOST = "host_lost"
STOP_HOST_ENDED = "host_ended"
STOP_UNCLAIMED = "unclaimed"
STOP_START_FAILED = "start_failed"


class ComputeSession(Base, TimestampMixin):
    __tablename__ = "compute_sessions"
    __table_args__ = (
        Index("ix_compute_sessions_status", "status"),
        Index("ix_compute_sessions_customer_status", "customer_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=SESSION_PENDING)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    kit_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)  # "slug@version"
    image_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="pty")  # pty | tcp
    tcp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)  # container loopback port (tcp mode)
    command: Mapped[list | None] = mapped_column(JSON, nullable=True)  # pty command argv, from the kit manifest
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    isolation_tier: Mapped[str] = mapped_column(String(16), nullable=False, default="container")
    network_policy: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # B5, {"egress": [...]} or None
    max_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    idle_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_price_per_gpu_hour: Mapped[decimal.Decimal] = mapped_column(Numeric(18, 6), nullable=False)

    # Money. The hold is the worst case (max price x max duration + fee), taken once at create.
    held_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    captured_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_per_gpu_hour: Mapped[decimal.Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)  # set at claim
    active_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    machine_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    provider_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    # End-to-end key directory (public keys only).
    client_pubkey: Mapped[str] = mapped_column(String(64), nullable=False)  # browser ephemeral, given at create
    host_pubkey: Mapped[str | None] = mapped_column(String(64), nullable=True)  # agent per-session, given at claim

    # Single-use, short-lived relay tokens (sha256 hex of the secret).
    host_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    host_token_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    host_token_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    browser_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    browser_token_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    browser_token_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_host_heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
