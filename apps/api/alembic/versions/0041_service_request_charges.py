"""U11 per-request billing accrual table and services.max_concurrency.

Revision ID: 0041_service_request_charges
Revises: 0040_sealed_verdict
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041_service_request_charges"
down_revision: Union[str, None] = "0040_sealed_verdict"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("services", sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="1"))
    op.create_table(
        "service_request_charges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False),
        sa.Column("replica_id", sa.Uuid(), sa.ForeignKey("service_replicas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_id", sa.Uuid(), sa.ForeignKey("service_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_seconds", sa.Numeric(14, 3), nullable=False),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("gpu_seconds", sa.Numeric(16, 3), nullable=False),
        sa.Column("rate_multiplier", sa.Numeric(10, 4), nullable=False),
        sa.Column("batch_key", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("request_id", name="uq_service_request_charges_request"),
    )
    op.create_index("ix_service_request_charges_service_id", "service_request_charges", ["service_id"])
    op.create_index("ix_service_request_charges_replica", "service_request_charges", ["replica_id", "batch_key"])


def downgrade() -> None:
    op.drop_index("ix_service_request_charges_replica", table_name="service_request_charges")
    op.drop_index("ix_service_request_charges_service_id", table_name="service_request_charges")
    op.drop_table("service_request_charges")
    op.drop_column("services", "max_concurrency")
