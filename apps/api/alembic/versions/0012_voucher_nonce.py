"""Persist the voucher nonce an escrow's signature was minted at.

ComputeSettlement binds every voucher to the escrow's current ``nonce`` and
rejects anything else with "Stale voucher nonce". The signer used a hardcoded
0, which only ever matches a fresh escrow. The moment an operator calls
``invalidateVouchers`` — the emergency "that voucher was wrong, kill it" lever,
which bumps the nonce — the escrow became permanently unsettleable: every
re-issued voucher still said 0 and reverted on chain.

Storing the nonce lets the signer mint at the live value and lets a re-issue be
told apart from a duplicate.

Revision ID: 0012_voucher_nonce
Revises: 0011_phases_bcd
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_voucher_nonce"
down_revision: Union[str, None] = "0011_phases_bcd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable + server_default 0: existing rows were signed at nonce 0, which
    # is exactly what the old hardcoded constant produced, so backfilling 0 is
    # factually correct rather than a guess.
    op.add_column(
        "on_chain_escrows",
        sa.Column(
            "voucher_nonce",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("on_chain_escrows", "voucher_nonce")
