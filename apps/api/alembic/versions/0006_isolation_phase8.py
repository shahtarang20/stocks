"""phase 8 isolation: workload isolation tiers and machine remote attestation

Revision ID: 0006_isolation_phase8
Revises: 0005_billing_phase5
Create Date: 2026-10-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_isolation_phase8"
down_revision: Union[str, None] = "0005_billing_phase5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add isolation_tier to jobs
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "isolation_tier",
                sa.String(length=32),
                nullable=False,
                server_default="container",
            )
        )

    # 2. Add supported_isolation_tiers to provider_capabilities
    with op.batch_alter_table("provider_capabilities") as batch_op:
        batch_op.add_column(
            sa.Column(
                "supported_isolation_tiers",
                sa.JSON(),
                nullable=False,
                server_default='["container"]',
            )
        )

    # 3. Create machine_attestations table
    op.create_table(
        "machine_attestations",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "machine_id",
            sa.Uuid(),
            sa.ForeignKey("provider_machines.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("isolation_tier", sa.String(length=32), nullable=False, server_default="confidential"),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("pcr_measurements", sa.JSON(), nullable=False),
        sa.Column("platform_quote", sa.String(length=1024), nullable=False),
        sa.Column("signature", sa.String(length=512), nullable=False),
        sa.Column("verification_status", sa.String(length=32), nullable=False, server_default="verified"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("machine_attestations")
    with op.batch_alter_table("provider_capabilities") as batch_op:
        batch_op.drop_column("supported_isolation_tiers")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("isolation_tier")
