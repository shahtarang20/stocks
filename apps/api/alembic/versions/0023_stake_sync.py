"""Stake sync: machine stake flags and the stake_sync_runs audit table (A1).

Revision ID: 0023_stake_sync
Revises: 0022_amd_gpu_support
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_stake_sync"
down_revision: Union[str, None] = "0022_amd_gpu_support"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("provider_machines") as batch_op:
        batch_op.add_column(
            sa.Column("stake_unverified", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("stake_synced_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "stake_sync_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("chain_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("block_number", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("machines_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("machines_changed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_stake_sync_runs_chain_started", "stake_sync_runs", ["chain_id", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_stake_sync_runs_chain_started", table_name="stake_sync_runs")
    op.drop_table("stake_sync_runs")
    with op.batch_alter_table("provider_machines") as batch_op:
        batch_op.drop_column("stake_synced_at")
        batch_op.drop_column("stake_unverified")
