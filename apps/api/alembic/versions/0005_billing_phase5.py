"""phase 5 billing: double-entry ledger, invoices, payments, refunds, payouts, disputes

Revision ID: 0005_billing_phase5
Revises: 0004_jobs_phase4
Create Date: 2026-10-17

Creates the Phase 5 money-layer tables AND seeds the singleton platform-side
ledger accounts (external funding, holding escrow, platform_revenue, payout_pending)
with deterministic ids so prod has them from the first migration. Reversible.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_billing_phase5"
down_revision: Union[str, None] = "0004_jobs_phase4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CURRENCY = "USDC_CREDITS"

# Deterministic ids for the seeded singleton platform accounts.
_EXTERNAL_ID = uuid.UUID("b0000000-0000-4000-8000-000000000001")
_HOLDING_ID = uuid.UUID("b0000000-0000-4000-8000-000000000002")
_PLATFORM_REVENUE_ID = uuid.UUID("b0000000-0000-4000-8000-000000000003")
_PAYOUT_PENDING_ID = uuid.UUID("b0000000-0000-4000-8000-000000000004")


def upgrade() -> None:
    billing_accounts = op.create_table(
        "billing_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("owner_type", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=32), nullable=False),
        sa.Column("balance_minor", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_type", "owner_id", "type", name="uq_billing_accounts_owner_type"),
    )
    op.create_index("ix_billing_accounts_owner_id", "billing_accounts", ["owner_id"])
    op.create_index("ix_billing_accounts_type", "billing_accounts", ["type"])

    op.create_table(
        "ledger_transactions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reference_type", sa.String(length=32), nullable=True),
        sa.Column("reference_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_ledger_transactions_idem"),
    )
    op.create_index("ix_ledger_transactions_kind", "ledger_transactions", ["kind"])
    op.create_index("ix_ledger_transactions_reference_id", "ledger_transactions", ["reference_id"])
    op.create_index("ix_ledger_transactions_created_at", "ledger_transactions", ["created_at"])

    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["transaction_id"], ["ledger_transactions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["billing_accounts.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_ledger_entries_account_id", "ledger_entries", ["account_id"])
    op.create_index("ix_ledger_entries_transaction_id", "ledger_entries", ["transaction_id"])

    op.create_table(
        "usage_records",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("estimated_minutes", sa.BigInteger(), nullable=False),
        sa.Column("actual_seconds", sa.BigInteger(), nullable=False),
        sa.Column("estimated_cost_minor", sa.BigInteger(), nullable=False),
        sa.Column("actual_cost_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_usage_records_job_id", "usage_records", ["job_id"])
    op.create_index("ix_usage_records_customer_id", "usage_records", ["customer_id"])

    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("line_items", sa.JSON(), nullable=False),
        sa.Column("estimated_total_minor", sa.BigInteger(), nullable=False),
        sa.Column("actual_total_minor", sa.BigInteger(), nullable=False),
        sa.Column("platform_fee_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("job_id", name="uq_invoices_job_id"),
    )
    op.create_index("ix_invoices_job_id", "invoices", ["job_id"])
    op.create_index("ix_invoices_customer_id", "invoices", ["customer_id"])

    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_configuration", sa.String(length=32), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transaction_id"], ["ledger_transactions.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_payments_customer_id", "payments", ["customer_id"])

    op.create_table(
        "refunds",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("invoice_id", sa.Uuid(), nullable=True),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("transaction_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["customer_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transaction_id"], ["ledger_transactions.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_refunds_job_id", "refunds", ["job_id"])
    op.create_index("ix_refunds_invoice_id", "refunds", ["invoice_id"])
    op.create_index("ix_refunds_customer_id", "refunds", ["customer_id"])

    op.create_table(
        "provider_payouts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("request_txn_id", sa.Uuid(), nullable=True),
        sa.Column("settle_txn_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["request_txn_id"], ["ledger_transactions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["settle_txn_id"], ["ledger_transactions.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_provider_payouts_provider_id", "provider_payouts", ["provider_id"])

    op.create_table(
        "disputes",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("resolution", sa.String(length=1024), nullable=True),
        sa.Column("refund_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_disputes_job_id", "disputes", ["job_id"])
    op.create_index("ix_disputes_customer_id", "disputes", ["customer_id"])

    # Seed the singleton platform-side accounts.
    now = dt.datetime.now(dt.timezone.utc)

    def _acct(acct_id, owner_type, acct_type):
        return {
            "id": acct_id,
            "owner_type": owner_type,
            "owner_id": None,
            "type": acct_type,
            "currency": CURRENCY,
            "balance_minor": 0,
            "created_at": now,
        }

    op.bulk_insert(
        billing_accounts,
        [
            _acct(_EXTERNAL_ID, "external", "external"),
            _acct(_HOLDING_ID, "holding", "holding"),
            _acct(_PLATFORM_REVENUE_ID, "platform", "platform_revenue"),
            _acct(_PAYOUT_PENDING_ID, "platform", "payout_pending"),
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_disputes_customer_id", table_name="disputes")
    op.drop_index("ix_disputes_job_id", table_name="disputes")
    op.drop_table("disputes")

    op.drop_index("ix_provider_payouts_provider_id", table_name="provider_payouts")
    op.drop_table("provider_payouts")

    op.drop_index("ix_refunds_customer_id", table_name="refunds")
    op.drop_index("ix_refunds_invoice_id", table_name="refunds")
    op.drop_index("ix_refunds_job_id", table_name="refunds")
    op.drop_table("refunds")

    op.drop_index("ix_payments_customer_id", table_name="payments")
    op.drop_table("payments")

    op.drop_index("ix_invoices_customer_id", table_name="invoices")
    op.drop_index("ix_invoices_job_id", table_name="invoices")
    op.drop_table("invoices")

    op.drop_index("ix_usage_records_customer_id", table_name="usage_records")
    op.drop_index("ix_usage_records_job_id", table_name="usage_records")
    op.drop_table("usage_records")

    op.drop_index("ix_ledger_entries_transaction_id", table_name="ledger_entries")
    op.drop_index("ix_ledger_entries_account_id", table_name="ledger_entries")
    op.drop_table("ledger_entries")

    op.drop_index("ix_ledger_transactions_created_at", table_name="ledger_transactions")
    op.drop_index("ix_ledger_transactions_reference_id", table_name="ledger_transactions")
    op.drop_index("ix_ledger_transactions_kind", table_name="ledger_transactions")
    op.drop_table("ledger_transactions")

    op.drop_index("ix_billing_accounts_type", table_name="billing_accounts")
    op.drop_index("ix_billing_accounts_owner_id", table_name="billing_accounts")
    op.drop_table("billing_accounts")
