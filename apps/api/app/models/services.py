"""Services / endpoints (U11, docs/architecture/U11-services-design.md): services, their replicas (each an ordinary
job) and the pull-based request queue. No column is added to an existing table."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, utcnow

# Service.status
SERVICE_ACTIVE = "active"
SERVICE_PAUSED = "paused"
SERVICE_DELETED = "deleted"

# Service.kind
KIND_GENERIC = "generic"
KIND_INFERENCE = "inference"

# ServiceReplica.status
REPLICA_STARTING = "starting"  # job created, not yet reported healthy
REPLICA_HEALTHY = "healthy"  # routable
REPLICA_DRAINING = "draining"  # told to stop; finishes in-flight requests
REPLICA_DEAD = "dead"  # heartbeat missed: out of routing
REPLICA_STOPPED = "stopped"  # job ended normally
REPLICA_LIVE = (REPLICA_STARTING, REPLICA_HEALTHY, REPLICA_DRAINING)

# ServiceRequest.status
REQ_QUEUED = "queued"
REQ_ASSIGNED = "assigned"
REQ_DONE = "done"
REQ_FAILED = "failed"
REQ_EXPIRED = "expired"
REQ_OPEN = (REQ_QUEUED, REQ_ASSIGNED)


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (Index("ix_services_customer_status", "customer_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=KIND_GENERIC)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=SERVICE_ACTIVE)
    spec: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # Job Spec v2 (Kit) as given
    kit_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    isolation_tier: Mapped[str] = mapped_column(String(32), nullable=False, default="container")
    required_gpu_runtime: Mapped[str] = mapped_column(String(32), nullable=False, default="cuda")
    min_replicas: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_replicas: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    scale_to_zero: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=8000)  # container port the agent forwards to
    health_path: Mapped[str] = mapped_column(String(128), nullable=False, default="/health")
    request_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # parallel requests per replica (cap 64)
    max_total_cost: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)  # customer's spend cap
    desired_replicas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    capped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_request_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_scaled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ServiceReplica(Base):
    __tablename__ = "service_replicas"
    __table_args__ = (Index("ix_service_replicas_service_status", "service_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("services.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True)
    machine_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("provider_machines.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=REPLICA_STARTING)
    window_ends_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    authorised_cost: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    healthy_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    in_flight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ServiceRequest(Base):
    __tablename__ = "service_requests"
    __table_args__ = (
        UniqueConstraint("service_id", "idempotency_key", name="uq_service_requests_idem"),
        Index("ix_service_requests_service_status", "service_id", "status", "queued_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("services.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    replica_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("service_replicas.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=REQ_QUEUED)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False, default="POST")
    path: Mapped[str] = mapped_column(String(256), nullable=False, default="/")
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="application/json")
    body_b64: Mapped[str] = mapped_column(String, nullable=False, default="")
    no_retry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # times handed to a replica
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response_b64: Mapped[str | None] = mapped_column(String, nullable=True)
    response_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    queued_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    assigned_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ServiceRequestCharge(Base):
    """Per-request billing accrual (SERVICE_BILLING_MODE=per_request). One row per completed request (unique request_id =
    idempotent accrual). ``batch_key`` is set when the replica's batch is closed at its job settlement; it is the
    idempotency key of that batch. Settlement itself is the existing job billing (no ledger logic here)."""

    __tablename__ = "service_request_charges"
    __table_args__ = (Index("ix_service_request_charges_replica", "replica_id", "batch_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True)
    replica_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("service_replicas.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("service_requests.id", ondelete="CASCADE"), nullable=False, unique=True)
    service_seconds: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    gpu_seconds: Mapped[decimal.Decimal] = mapped_column(Numeric(16, 3), nullable=False)  # service_seconds * gpu_count
    rate_multiplier: Mapped[decimal.Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=1)
    batch_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    settled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
