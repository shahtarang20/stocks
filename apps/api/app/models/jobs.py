"""Job execution models (Phase 4): jobs + FSM events + attempts + logs + artifacts.

Portable column types only (Uuid, JSON, Numeric, DateTime(timezone=True),
String/Integer/Boolean) so the models run on SQLite (tests) and Postgres (prod).
Money is ``Numeric``; specs/outputs/verification are ``JSON``.

FSM state names are stored as lowercase checked-varchar (see app.services.job_fsm).
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
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, utcnow

# Actors that may drive an FSM transition.
EVENT_ACTORS = ("customer", "scheduler", "agent", "system")


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"
    __table_args__ = (
        # Idempotency is per-customer: a customer may reuse a key only to mean
        # "the same job". NULL keys are unconstrained (no implicit dedupe).
        UniqueConstraint(
            "customer_id", "idempotency_key", name="uq_jobs_customer_idempotency_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )

    workload_type: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created", index=True)

    machine_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("provider_machines.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("resource_reservations.id", ondelete="SET NULL"), nullable=True
    )

    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_cost: Mapped[decimal.Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    max_runtime_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    retry_policy: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    attempts_made: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)

    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="simulated")
    isolation_tier: Mapped[str] = mapped_column(String(32), nullable=False, default="container")
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Phase B/C: batching, challenges and redundant (shadow) execution ---
    # Set when this job was submitted as part of a bulk batch.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("batches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # True for an injected known-answer challenge. Deliberately NOT exposed to
    # the agent API: a provider that can see this flag can cheat the audit.
    is_challenge: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Set on a shadow re-execution; points at the primary job it duplicates.
    shadow_of_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    match_explanation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    final_cost: Mapped[decimal.Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    events: Mapped[list["JobEvent"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobEvent.seq"
    )


class JobEvent(Base):
    """Append-only FSM transition record. Immutable once written."""

    __tablename__ = "job_events"
    __table_args__ = (
        UniqueConstraint("job_id", "seq", name="uq_job_events_job_id_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor: Mapped[str] = mapped_column(String(32), nullable=False, default="system")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    job: Mapped["Job"] = relationship(back_populates="events")


class JobAssignment(Base):
    """A dispatch of a job to a machine (one row per reservation hold)."""

    __tablename__ = "job_assignments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    machine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("provider_machines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("resource_reservations.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    released_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobAttempt(Base):
    """One execution attempt (claim -> lease). Records outcome per attempt."""

    __tablename__ = "job_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    machine_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("provider_machines.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobLogMetadata(Base):
    """Metadata for a log chunk stored in object storage (bytes never in the DB)."""

    __tablename__ = "job_logs_metadata"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Artifact(Base):
    """A stored output blob. The API only stores/serves bytes — never executes them."""

    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="output")
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
