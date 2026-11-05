"""Unit quotes and learned-correction samples (U15). Additive; no existing table changes.

Revision ID: 0029_unit_quotes
Revises: 0028_job_spec_v2
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_unit_quotes"
down_revision: Union[str, None] = "0028_job_spec_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "unit_quotes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kit_slug", sa.String(64), nullable=False),
        sa.Column("kit_version", sa.String(32), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("expected_units", sa.BigInteger(), nullable=False),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("max_cost", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("confidence", sa.String(8), nullable=False),
        sa.Column("capped", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_unit_quotes_customer_id", "unit_quotes", ["customer_id"])
    op.create_index("ix_unit_quotes_job_id", "unit_quotes", ["job_id"])
    op.create_table(
        "unit_quote_samples",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kit_slug", sa.String(64), nullable=False),
        sa.Column("kit_version", sa.String(32), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("units", sa.BigInteger(), nullable=False),
        sa.Column("estimated_gpu_seconds", sa.Numeric(18, 4), nullable=False),
        sa.Column("actual_gpu_seconds", sa.Numeric(18, 4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_unit_quote_samples_kit", "unit_quote_samples", ["kit_slug", "kit_version", "unit"])


def downgrade() -> None:
    op.drop_index("ix_unit_quote_samples_kit", table_name="unit_quote_samples")
    op.drop_table("unit_quote_samples")
    op.drop_index("ix_unit_quotes_job_id", table_name="unit_quotes")
    op.drop_index("ix_unit_quotes_customer_id", table_name="unit_quotes")
    op.drop_table("unit_quotes")
