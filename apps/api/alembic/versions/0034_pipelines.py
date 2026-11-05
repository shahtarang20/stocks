"""Pipelines (U9): pipeline_runs and pipeline_steps. No column added to existing tables.

Revision ID: 0034_pipelines
Revises: 0033_sessions
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034_pipelines"
down_revision: Union[str, None] = "0033_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("edges", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("isolation_tier", sa.String(16), nullable=False, server_default="container"),
        sa.Column("required_gpu_runtime", sa.String(8), nullable=False, server_default="cuda"),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("restart_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outputs", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(96), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pipeline_runs_customer_id", "pipeline_runs", ["customer_id"])
    op.create_index("ix_pipeline_runs_status", "pipeline_runs", ["status"])
    op.create_index("ix_pipeline_runs_customer_idem", "pipeline_runs", ["customer_id", "idempotency_key"])

    op.create_table(
        "pipeline_steps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_key", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("needs", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outputs", sa.JSON(), nullable=True),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(96), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("run_id", "step_key", name="uq_pipeline_steps_run_key"),
    )
    op.create_index("ix_pipeline_steps_run_id", "pipeline_steps", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_steps_run_id", table_name="pipeline_steps")
    op.drop_table("pipeline_steps")
    op.drop_index("ix_pipeline_runs_customer_idem", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_status", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_customer_id", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
