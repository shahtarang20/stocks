"""Pipelines (U9, docs/architecture/U9-pipelines-design.md): a DAG of v2 kit jobs / fan-outs linked by data-plane files."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, utcnow

# PipelineRun.status
RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_ACTIVE = (RUN_PENDING, RUN_RUNNING)

# PipelineStep.status
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_CANCELLED = "cancelled"

# PipelineStep.kind
KIND_JOB = "job"
KIND_FANOUT = "fanout"


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (
        Index("ix_pipeline_runs_status", "status"),
        Index("ix_pipeline_runs_customer_idem", "customer_id", "idempotency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RUN_PENDING)
    spec: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # the pipeline as submitted
    edges: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # [{from_step, output, to_step, input}]
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    isolation_tier: Mapped[str] = mapped_column(String(16), nullable=False, default="container")
    required_gpu_runtime: Mapped[str] = mapped_column(String(8), nullable=False, default="cuda")
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    restart_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outputs: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {step: {output name: file info}} of sink steps
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PipelineStep(Base):
    __tablename__ = "pipeline_steps"
    __table_args__ = (UniqueConstraint("run_id", "step_key", name="uq_pipeline_steps_run_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_key: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False, default=KIND_JOB)
    spec: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # the step's raw Job Spec v2
    needs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # [{from_step, output, input}]
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STEP_PENDING)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outputs: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {name: {file_id, sha256, size_bytes}}
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # past attempts
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
