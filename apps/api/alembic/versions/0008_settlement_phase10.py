"""phase 10 settlement: on-chain escrow locks and smart contract reconciliation

Revision ID: 0008_settlement_phase10
Revises: 0007_telemetry_phase9
Create Date: 2026-10-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_settlement_phase10"
down_revision: Union[str, None] = "0007_telemetry_phase9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. on_chain_escrows table
    op.create_table(
        "on_chain_escrows",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "provider_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("chain_id", sa.BigInteger(), nullable=False, server_default="31337"),
        sa.Column("contract_address", sa.String(length=42), nullable=False),
        sa.Column("token_address", sa.String(length=42), nullable=False),
        sa.Column("job_bytes32", sa.String(length=66), nullable=False, index=True),
        sa.Column("customer_address", sa.String(length=42), nullable=True),
        sa.Column("provider_address", sa.String(length=42), nullable=True),
        sa.Column("locked_amount_units", sa.String(length=78), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="locked", index=True),
        sa.Column("lock_tx_hash", sa.String(length=66), nullable=False, index=True),
        sa.Column("settle_tx_hash", sa.String(length=66), nullable=True),
        sa.Column("refund_tx_hash", sa.String(length=66), nullable=True),
        sa.Column("final_cost_units", sa.String(length=78), nullable=True),
        sa.Column("platform_fee_units", sa.String(length=78), nullable=True),
        sa.Column("voucher_signature", sa.String(length=132), nullable=True),
        sa.Column("reconciled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_on_chain_escrows_status_deadline",
        "on_chain_escrows",
        ["status", "deadline"],
    )
    op.create_index(
        "ix_on_chain_escrows_contract_job",
        "on_chain_escrows",
        ["contract_address", "job_bytes32"],
    )

    # 2. on_chain_transactions table
    op.create_table(
        "on_chain_transactions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("tx_hash", sa.String(length=66), unique=True, nullable=False, index=True),
        sa.Column("tx_type", sa.String(length=32), nullable=False, index=True),
        sa.Column("sender_address", sa.String(length=42), nullable=False, index=True),
        sa.Column("recipient_address", sa.String(length=42), nullable=True),
        sa.Column("amount_units", sa.String(length=78), nullable=True),
        sa.Column("token_symbol", sa.String(length=16), nullable=False, server_default="COMPUTE"),
        sa.Column("block_number", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="confirmed", index=True),
        sa.Column("reconciled_with_ledger", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "ledger_txn_id",
            sa.Uuid(),
            sa.ForeignKey("ledger_transactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_on_chain_tx_chain_sender",
        "on_chain_transactions",
        ["chain_id", "sender_address"],
    )


def downgrade() -> None:
    op.drop_table("on_chain_transactions")
    op.drop_table("on_chain_escrows")
