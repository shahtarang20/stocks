"""Add Stripe fields to payments and refunds.

Revision ID: 0010_stripe_billing_phase5
Revises: 0009_hardware_model_phase4
Create Date: 2026-10-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_stripe_billing_phase5"
down_revision: Union[str, None] = "0009_hardware_model_phase4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("external_reference", sa.String(length=255), nullable=True))
    op.add_column("payments", sa.Column("stripe_payment_intent_id", sa.String(length=255), nullable=True))
    op.add_column("payments", sa.Column("stripe_session_id", sa.String(length=255), nullable=True))
    op.add_column("payments", sa.Column("metadata_json", sa.JSON(), nullable=True))
    op.create_index("ix_payments_external_reference", "payments", ["external_reference"])
    op.create_index("ix_payments_stripe_payment_intent_id", "payments", ["stripe_payment_intent_id"])
    op.create_index("ix_payments_stripe_session_id", "payments", ["stripe_session_id"])

    op.add_column("refunds", sa.Column("stripe_refund_id", sa.String(length=255), nullable=True))
    op.create_index("ix_refunds_stripe_refund_id", "refunds", ["stripe_refund_id"])


def downgrade() -> None:
    op.drop_index("ix_refunds_stripe_refund_id", table_name="refunds")
    op.drop_column("refunds", "stripe_refund_id")

    op.drop_index("ix_payments_stripe_session_id", table_name="payments")
    op.drop_index("ix_payments_stripe_payment_intent_id", table_name="payments")
    op.drop_index("ix_payments_external_reference", table_name="payments")
    op.drop_column("payments", "metadata_json")
    op.drop_column("payments", "stripe_session_id")
    op.drop_column("payments", "stripe_payment_intent_id")
    op.drop_column("payments", "external_reference")
