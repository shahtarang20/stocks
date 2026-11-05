"""Fan-out (U4, docs/architecture/U4-fan-out-design.md): a parent job split into parts, each part run by child jobs.

A part is a slot; a run is one child job working on that slot. A re-run or a straggler copy is a NEW run (a new
ordinary v2 job with its own hold, metering and invoice), never an edit of an existing job.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, utcnow

# FanoutGroup.status
GROUP_RUNNING = "running"
GROUP_MERGING = "merging"
GROUP_COMPLETED = "completed"
GROUP_FAILED = "failed"
GROUP_CANCELLED = "cancelled"
GROUP_ACTIVE = (GROUP_RUNNING, GROUP_MERGING)

# FanoutPart.status
PART_PENDING = "pending"  # no live run (not started yet, or waiting for a re-run)
PART_RUNNING = "running"
PART_DONE = "done"
PART_FAILED = "failed"  # attempts exhausted
PART_CANCELLED = "cancelled"

# FanoutPartRun.kind / status
RUN_PRIMARY = "primary"
RUN_RETRY = "retry"
RUN_STRAGGLER = "straggler"
RUN_ACTIVE = "active"
RUN_WON = "won"
RUN_LOST = "lost"  # the other copy won; this one was cancelled
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"


class FanoutGroup(Base):
    __tablename__ = "fanout_groups"
    __table_args__ = (Index("ix_fanout_groups_status", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    parent_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=GROUP_RUNNING)
    part_count: Mapped[int] = mapped_column(Integer, nullable=False)
    split: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # the customer's rule, as given
    # {"outputs": [names], "max_parallel": int, "straggler_multiple": float, "max_part_attempts": int}
    plan: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    merge_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    merge_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FanoutPart(Base):
    __tablename__ = "fanout_parts"
    __table_args__ = (UniqueConstraint("group_id", "idx", name="uq_fanout_parts_group_idx"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("fanout_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idx: Mapped[int] = mapped_column(Integer, nullable=False)  # 0-based position; merge order
    patch: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # {param name: value} applied to params
    weight: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)  # units in this part (cost share)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PART_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # primary + retry runs started
    winner_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    duration_seconds: Mapped[decimal.Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    final_cost: Mapped[decimal.Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class FanoutPartRun(Base):
    __tablename__ = "fanout_part_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    part_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("fanout_parts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=RUN_PRIMARY)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RUN_ACTIVE)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
