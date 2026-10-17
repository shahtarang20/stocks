"""phase 3 marketplace: pricing rules, quotes, resource reservations

Revision ID: 0003_marketplace_phase3
Revises: 0002_provider_phase2
Create Date: 2026-10-16

Seeds one active default pricing_rules row (platform_fee_rate=0.10) so the
pricing service always has an active rate to read.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_marketplace_phase3"
down_revision: Union[str, None] = "0002_provider_phase2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Deterministic id for the seeded default rule.
_DEFAULT_RULE_ID = uuid.UUID("10956783-1ffd-4d8e-82de-21486467a413")


def upgrade() -> None:
    pricing_rules = op.create_table(
        "pricing_rules",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("platform_fee_rate", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pricing_rules_active", "pricing_rules", ["active"])

    op.create_table(
        "quotes",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("machine_id", sa.Uuid(), nullable=False),
        sa.Column("workload_type", sa.String(length=64), nullable=False),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("estimated_minutes", sa.Integer(), nullable=False),
        sa.Column("price_per_hour_snapshot", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("platform_fee_rate", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("gpu_cost", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("platform_fee", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("total", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=32), nullable=False),
        sa.Column("max_authorized_spend", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["machine_id"], ["provider_machines.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_quotes_customer_id", "quotes", ["customer_id"])
    op.create_index("ix_quotes_machine_id", "quotes", ["machine_id"])
    op.create_index("ix_quotes_status", "quotes", ["status"])

    op.create_table(
        "resource_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("quote_id", sa.Uuid(), nullable=False),
        sa.Column("machine_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("slots", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["machine_id"], ["provider_machines.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("quote_id", name="uq_resource_reservations_quote_id"),
    )
    op.create_index(
        "ix_resource_reservations_machine_id", "resource_reservations", ["machine_id"]
    )
    op.create_index(
        "ix_resource_reservations_customer_id", "resource_reservations", ["customer_id"]
    )
    op.create_index(
        "ix_resource_reservations_status", "resource_reservations", ["status"]
    )

    # Seed one active default pricing rule (platform_fee_rate = 0.10).
    now = dt.datetime.now(dt.timezone.utc)
    op.bulk_insert(
        pricing_rules,
        [
            {
                "id": _DEFAULT_RULE_ID,
                "name": "default",
                "platform_fee_rate": "0.10",
                "active": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_resource_reservations_status", table_name="resource_reservations")
    op.drop_index("ix_resource_reservations_customer_id", table_name="resource_reservations")
    op.drop_index("ix_resource_reservations_machine_id", table_name="resource_reservations")
    op.drop_table("resource_reservations")

    op.drop_index("ix_quotes_status", table_name="quotes")
    op.drop_index("ix_quotes_machine_id", table_name="quotes")
    op.drop_index("ix_quotes_customer_id", table_name="quotes")
    op.drop_table("quotes")

    op.drop_index("ix_pricing_rules_active", table_name="pricing_rules")
    op.drop_table("pricing_rules")
