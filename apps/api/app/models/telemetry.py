"""Phase 9: High-volume time-series telemetry and alerting models.

Stores per-second to per-minute hardware utilization, temperature, memory,
throughput metrics, and threshold alerts.
Designed for high throughput with compound time-series indexes and compatible
with TimescaleDB hypertables.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, uuid_pk, utcnow


class MachineTelemetry(Base):
    """High-frequency machine / GPU telemetry time-series points."""

    __tablename__ = "machine_telemetry"

    id: Mapped[uuid.UUID] = uuid_pk()
    machine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("provider_machines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        index=True,
    )
    gpu_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gpu_util_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_util_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_used_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mem_total_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    fan_speed_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    extra_metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_machine_telemetry_machine_ts", "machine_id", "timestamp"),
        Index("ix_machine_telemetry_machine_gpu_ts", "machine_id", "gpu_index", "timestamp"),
    )


class JobTelemetry(Base):
    """Execution performance and resource utilization time-series for jobs."""

    __tablename__ = "job_telemetry"

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    machine_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("provider_machines.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        index=True,
    )
    gpu_util_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_used_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_per_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    step_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    extra_metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_job_telemetry_job_ts", "job_id", "timestamp"),
    )


class TelemetryAlert(Base):
    """Automated operational threshold alerts (e.g. overheating, memory leaks)."""

    __tablename__ = "telemetry_alerts"

    id: Mapped[uuid.UUID] = uuid_pk()
    machine_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("provider_machines.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="warning")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        index=True,
    )

    __table_args__ = (
        Index("ix_telemetry_alerts_status_sev", "status", "severity"),
        Index("ix_telemetry_alerts_machine_status", "machine_id", "status"),
    )
