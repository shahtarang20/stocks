"""Fan-out tables (U4): fanout_groups, fanout_parts, fanout_part_runs. No column added to existing tables.

Revision ID: 0029_fanout
Revises: 0028_job_spec_v2
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_fanout"
down_revision: Union[str, None] = "0028_job_spec_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fanout_groups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("parent_job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("part_count", sa.Integer(), nullable=False),
        sa.Column("split", sa.JSON(), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("merge_result", sa.JSON(), nullable=True),
        sa.Column("merge_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_fanout_groups_customer_id", "fanout_groups", ["customer_id"])
    op.create_index("ix_fanout_groups_status", "fanout_groups", ["status"])

    op.create_table(
        "fanout_parts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("group_id", sa.Uuid(), sa.ForeignKey("fanout_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("patch", sa.JSON(), nullable=False),
        sa.Column("weight", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("winner_job_id", sa.Uuid(), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(14, 3), nullable=True),
        sa.Column("final_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.UniqueConstraint("group_id", "idx", name="uq_fanout_parts_group_idx"),
    )
    op.create_index("ix_fanout_parts_group_id", "fanout_parts", ["group_id"])

    op.create_table(
        "fanout_part_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("part_id", sa.Uuid(), sa.ForeignKey("fanout_parts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_fanout_part_runs_part_id", "fanout_part_runs", ["part_id"])


def downgrade() -> None:
    op.drop_index("ix_fanout_part_runs_part_id", table_name="fanout_part_runs")
    op.drop_table("fanout_part_runs")
    op.drop_index("ix_fanout_parts_group_id", table_name="fanout_parts")
    op.drop_table("fanout_parts")
    op.drop_index("ix_fanout_groups_status", table_name="fanout_groups")
    op.drop_index("ix_fanout_groups_customer_id", table_name="fanout_groups")
    op.drop_table("fanout_groups")
