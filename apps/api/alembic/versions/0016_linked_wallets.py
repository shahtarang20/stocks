"""Linked wallets: an address a user proved they control, one per account per chain.

Revision ID: 0016_linked_wallets
Revises: 0015_profile_deletion
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_linked_wallets"
down_revision: Union[str, None] = "0015_profile_deletion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "linked_wallets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("address", sa.String(length=42), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chain_id", "address", name="uq_linked_wallets_chain_address"),
        sa.UniqueConstraint("user_id", "chain_id", name="uq_linked_wallets_user_chain"),
    )
    op.create_index("ix_linked_wallets_user_id", "linked_wallets", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_linked_wallets_user_id", table_name="linked_wallets")
    op.drop_table("linked_wallets")
