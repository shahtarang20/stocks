"""phase 2 provider onboarding & machine registration

Revision ID: 0002_provider_phase2
Revises: 0001_initial
Create Date: 2026-10-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_provider_phase2"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "providers",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("contact_email", sa.String(length=320), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_providers_user_id"),
    )
    op.create_index("ix_providers_user_id", "providers", ["user_id"], unique=True)

    op.create_table(
        "provider_machines",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("agent_version", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("availability", sa.String(length=32), nullable=False),
        sa.Column("asking_price_per_hour", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("max_concurrent_jobs", sa.Integer(), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=True),
        sa.Column("signing_key", sa.String(length=128), nullable=False),
        sa.Column("secret_hash", sa.String(length=128), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "provider_id", "hostname", name="uq_provider_machines_provider_hostname"
        ),
    )
    op.create_index(
        "ix_provider_machines_provider_id", "provider_machines", ["provider_id"]
    )
    op.create_index(
        "ix_provider_machines_secret_hash", "provider_machines", ["secret_hash"]
    )

    op.create_table(
        "hardware_inventory",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("machine_id", sa.Uuid(), nullable=False),
        sa.Column("verification_status", sa.String(length=32), nullable=False),
        sa.Column("cpu_model", sa.String(length=255), nullable=True),
        sa.Column("cpu_cores", sa.Integer(), nullable=False),
        sa.Column("ram_mb", sa.Integer(), nullable=False),
        sa.Column("disk_gb", sa.Integer(), nullable=False),
        sa.Column("gpu_model", sa.String(length=255), nullable=True),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("vram_mb", sa.Integer(), nullable=False),
        sa.Column("cuda_version", sa.String(length=64), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["machine_id"], ["provider_machines.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("machine_id", name="uq_hardware_inventory_machine_id"),
    )
    op.create_index(
        "ix_hardware_inventory_machine_id", "hardware_inventory", ["machine_id"], unique=True
    )

    op.create_table(
        "provider_capabilities",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("machine_id", sa.Uuid(), nullable=False),
        sa.Column("supported_workloads", sa.JSON(), nullable=False),
        sa.Column("verification_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["machine_id"], ["provider_machines.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("machine_id", name="uq_provider_capabilities_machine_id"),
    )
    op.create_index(
        "ix_provider_capabilities_machine_id",
        "provider_capabilities",
        ["machine_id"],
        unique=True,
    )

    op.create_table(
        "enrollment_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("machine_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["machine_id"], ["provider_machines.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("token_hash", name="uq_enrollment_tokens_token_hash"),
    )
    op.create_index(
        "ix_enrollment_tokens_provider_id", "enrollment_tokens", ["provider_id"]
    )
    op.create_index(
        "ix_enrollment_tokens_token_hash", "enrollment_tokens", ["token_hash"]
    )
    op.create_index(
        "ix_enrollment_tokens_machine_id", "enrollment_tokens", ["machine_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_enrollment_tokens_machine_id", table_name="enrollment_tokens")
    op.drop_index("ix_enrollment_tokens_token_hash", table_name="enrollment_tokens")
    op.drop_index("ix_enrollment_tokens_provider_id", table_name="enrollment_tokens")
    op.drop_table("enrollment_tokens")

    op.drop_index(
        "ix_provider_capabilities_machine_id", table_name="provider_capabilities"
    )
    op.drop_table("provider_capabilities")

    op.drop_index("ix_hardware_inventory_machine_id", table_name="hardware_inventory")
    op.drop_table("hardware_inventory")

    op.drop_index("ix_provider_machines_secret_hash", table_name="provider_machines")
    op.drop_index("ix_provider_machines_provider_id", table_name="provider_machines")
    op.drop_table("provider_machines")

    op.drop_index("ix_providers_user_id", table_name="providers")
    op.drop_table("providers")
