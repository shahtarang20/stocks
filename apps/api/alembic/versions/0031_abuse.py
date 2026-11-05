"""U16 abuse flags, submission holds and abuse reports (new tables only).

Revision ID: 0031_abuse
Revises: 0029_unit_quotes, 0029_fanout  (merges the two 0029 heads; re-point if 0030 lands)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031_abuse"
down_revision: Union[str, Sequence[str], None] = ("0029_unit_quotes", "0029_fanout")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "abuse_flags",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("signal", sa.String(64), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("enforced", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
    )
    op.create_index("ix_abuse_flags_job_id", "abuse_flags", ["job_id"])
    op.create_index("ix_abuse_flags_user_id", "abuse_flags", ["user_id"])
    op.create_index("ix_abuse_flags_status", "abuse_flags", ["status"])
    op.create_index("ix_abuse_flags_job_signal", "abuse_flags", ["job_id", "signal"])
    op.create_table(
        "abuse_holds",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("flag_id", sa.Uuid(), sa.ForeignKey("abuse_flags.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_abuse_holds_user_id", "abuse_holds", ["user_id"])
    op.create_table(
        "abuse_reports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("reporter_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actioned_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actioned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("action_note", sa.Text(), nullable=True),
    )
    op.create_index("ix_abuse_reports_job_id", "abuse_reports", ["job_id"])
    op.create_index("ix_abuse_reports_status", "abuse_reports", ["status"])


def downgrade() -> None:
    op.drop_table("abuse_reports")
    op.drop_table("abuse_holds")
    op.drop_table("abuse_flags")
