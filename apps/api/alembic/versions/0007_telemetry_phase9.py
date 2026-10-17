"""phase 9 telemetry: high-volume time-series telemetry and operational alerting

Revision ID: 0007_telemetry_phase9
Revises: 0006_isolation_phase8
Create Date: 2026-10-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_telemetry_phase9"
down_revision: Union[str, None] = "0006_isolation_phase8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create machine_telemetry table
    op.create_table(
        "machine_telemetry",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "machine_id",
            sa.Uuid(),
            sa.ForeignKey("provider_machines.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("gpu_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gpu_util_pct", sa.Float(), nullable=True),
        sa.Column("mem_util_pct", sa.Float(), nullable=True),
        sa.Column("mem_used_mb", sa.Integer(), nullable=True),
        sa.Column("mem_total_mb", sa.Integer(), nullable=True),
        sa.Column("temp_c", sa.Float(), nullable=True),
        sa.Column("power_w", sa.Float(), nullable=True),
        sa.Column("fan_speed_pct", sa.Float(), nullable=True),
        sa.Column("extra_metrics", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_machine_telemetry_machine_ts",
        "machine_telemetry",
        ["machine_id", "timestamp"],
    )
    op.create_index(
        "ix_machine_telemetry_machine_gpu_ts",
        "machine_telemetry",
        ["machine_id", "gpu_index", "timestamp"],
    )

    # 2. Create job_telemetry table
    op.create_table(
        "job_telemetry",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "machine_id",
            sa.Uuid(),
            sa.ForeignKey("provider_machines.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("gpu_util_pct", sa.Float(), nullable=True),
        sa.Column("mem_used_mb", sa.Integer(), nullable=True),
        sa.Column("tokens_per_sec", sa.Float(), nullable=True),
        sa.Column("step_count", sa.Integer(), nullable=True),
        sa.Column("loss", sa.Float(), nullable=True),
        sa.Column("extra_metrics", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_job_telemetry_job_ts",
        "job_telemetry",
        ["job_id", "timestamp"],
    )

    # 3. Create telemetry_alerts table
    op.create_table(
        "telemetry_alerts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "machine_id",
            sa.Uuid(),
            sa.ForeignKey("provider_machines.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("alert_type", sa.String(length=64), nullable=False, index=True),
        sa.Column("severity", sa.String(length=32), nullable=False, server_default="warning"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("metric_value", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active", index=True),
        sa.Column(
            "acknowledged_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.create_index(
        "ix_telemetry_alerts_status_sev",
        "telemetry_alerts",
        ["status", "severity"],
    )
    op.create_index(
        "ix_telemetry_alerts_machine_status",
        "telemetry_alerts",
        ["machine_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("telemetry_alerts")
    op.drop_table("job_telemetry")
    op.drop_table("machine_telemetry")
