"""Token withdrawals (B1): the withdrawals table. OFF by default (WITHDRAWALS_ENABLED).

Revision ID: 0025_withdrawals
Revises: 0024_job_network_policy
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_withdrawals"
down_revision: Union[str, None] = "0024_job_network_policy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "withdrawals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("token_address", sa.String(42), nullable=False),
        sa.Column("destination", sa.String(42), nullable=False),
        sa.Column("credits_minor", sa.BigInteger(), nullable=False),
        sa.Column("amount_raw", sa.String(80), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("hot_wallet", sa.String(42), nullable=True),
        sa.Column("nonce", sa.BigInteger(), nullable=True),
        sa.Column("tx_hash", sa.String(66), nullable=True),
        sa.Column("raw_tx", sa.Text(), nullable=True),
        sa.Column("receipt_block", sa.BigInteger(), nullable=True),
        sa.Column("broadcast_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("refund_reason", sa.String(64), nullable=True),
        sa.Column("request_txn_id", sa.Uuid(), nullable=True),
        sa.Column("final_txn_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_withdrawals_user_idem"),
    )
    op.create_index("ix_withdrawals_tx_hash", "withdrawals", ["tx_hash"])
    op.create_index("ix_withdrawals_status_created", "withdrawals", ["status", "created_at"])
    op.create_index("ix_withdrawals_user_created", "withdrawals", ["user_id", "created_at"])
    op.create_index(
        "uq_withdrawals_one_in_flight",
        "withdrawals",
        ["hot_wallet"],
        unique=True,
        postgresql_where=sa.text("status IN ('sending','broadcast')"),
        sqlite_where=sa.text("status IN ('sending','broadcast')"),
    )


def downgrade() -> None:
    op.drop_index("uq_withdrawals_one_in_flight", table_name="withdrawals")
    op.drop_index("ix_withdrawals_user_created", table_name="withdrawals")
    op.drop_index("ix_withdrawals_status_created", table_name="withdrawals")
    op.drop_index("ix_withdrawals_tx_hash", table_name="withdrawals")
    op.drop_table("withdrawals")
