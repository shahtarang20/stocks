"""Token deposits: one row per on-chain COMPUTE transfer received at the platform address.

Revision ID: 0017_token_deposits
Revises: 0016_linked_wallets
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_token_deposits"
down_revision: Union[str, None] = "0016_linked_wallets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "token_deposits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("tx_hash", sa.String(length=66), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("token_address", sa.String(length=42), nullable=False),
        sa.Column("from_address", sa.String(length=42), nullable=False),
        sa.Column("to_address", sa.String(length=42), nullable=False),
        sa.Column("amount_raw", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("credits_minor", sa.BigInteger(), nullable=True),
        sa.Column("ledger_txn_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chain_id", "tx_hash", "log_index", name="uq_token_deposits_event"),
    )
    op.create_index("ix_token_deposits_user_id", "token_deposits", ["user_id"])
    op.create_index("ix_token_deposits_user_created", "token_deposits", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_token_deposits_user_created", table_name="token_deposits")
    op.drop_index("ix_token_deposits_user_id", table_name="token_deposits")
    op.drop_table("token_deposits")
