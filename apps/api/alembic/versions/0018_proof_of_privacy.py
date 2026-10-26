"""ADR-0005: Proof-of-Privacy (transparency log, receipts, canaries, trusted images).

Revision ID: 0018_proof_of_privacy
Revises: 0017_token_deposits
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_proof_of_privacy"
down_revision: Union[str, None] = "0017_token_deposits"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add privacy columns to jobs
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("data_policy", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("encrypted_input", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("encrypted_output", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("dk_shares", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("value_at_risk", sa.Numeric(14, 2), nullable=True))
        batch_op.add_column(sa.Column("privacy_receipt_id", sa.Uuid(), nullable=True))

    # 2. Add privacy & staking columns to provider_machines
    with op.batch_alter_table("provider_machines") as batch_op:
        batch_op.add_column(
            sa.Column("stake_bonded", sa.Numeric(18, 2), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("memory_encryption_active", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("kyc_verified", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("tpm_ek_pub", sa.String(length=512), nullable=True))
        batch_op.add_column(
            sa.Column("is_banned", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    # 3. Create transparency_log table
    op.create_table(
        "transparency_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("leaf_index", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("leaf_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("merkle_root", sa.String(length=64), nullable=False),
        sa.Column("anchored_tx_hash", sa.String(length=66), nullable=True),
        sa.Column("anchored_block_number", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("leaf_index", name="uq_transparency_log_leaf_index"),
    )
    op.create_index("ix_transparency_log_event_type", "transparency_log", ["event_type"])

    # 4. Create privacy_receipts table
    op.create_table(
        "privacy_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("machine_id", sa.Uuid(), sa.ForeignKey("provider_machines.id", ondelete="SET NULL"), nullable=True),
        sa.Column("isolation_tier", sa.String(length=32), nullable=False),
        sa.Column("image_hash", sa.String(length=64), nullable=False),
        sa.Column("attestation_verdict", sa.JSON(), nullable=False),
        sa.Column("guardian_releases", sa.JSON(), nullable=False),
        sa.Column("wipe_receipt", sa.JSON(), nullable=True),
        sa.Column("merkle_root", sa.String(length=64), nullable=False),
        sa.Column("inclusion_proofs", sa.JSON(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", name="uq_privacy_receipts_job_id"),
    )
    op.create_index("ix_privacy_receipts_machine_id", "privacy_receipts", ["machine_id"])

    # 5. Create canary_jobs table
    op.create_table(
        "canary_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("machine_id", sa.Uuid(), sa.ForeignKey("provider_machines.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canary_token", sa.String(length=128), nullable=False),
        sa.Column("canary_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("canary_token", name="uq_canary_jobs_token"),
    )
    op.create_index("ix_canary_jobs_machine_id", "canary_jobs", ["machine_id"])

    # 6. Create trusted_images table
    op.create_table(
        "trusted_images",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("image_hash", sa.String(length=64), nullable=False),
        sa.Column("version_tag", sa.String(length=64), nullable=False),
        sa.Column("tier", sa.String(length=32), nullable=False),
        sa.Column("expected_pcrs", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("image_hash", name="uq_trusted_images_hash"),
    )


def downgrade() -> None:
    op.drop_table("trusted_images")
    op.drop_table("canary_jobs")
    op.drop_table("privacy_receipts")
    op.drop_table("transparency_log")

    with op.batch_alter_table("provider_machines") as batch_op:
        batch_op.drop_column("is_banned")
        batch_op.drop_column("tpm_ek_pub")
        batch_op.drop_column("kyc_verified")
        batch_op.drop_column("memory_encryption_active")
        batch_op.drop_column("stake_bonded")

    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("privacy_receipt_id")
        batch_op.drop_column("value_at_risk")
        batch_op.drop_column("dk_shares")
        batch_op.drop_column("encrypted_output")
        batch_op.drop_column("encrypted_input")
        batch_op.drop_column("data_policy")
