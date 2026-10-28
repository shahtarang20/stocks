"""TPM attestation-key enrolments bound to a machine's EK by credential activation (WP-F).

Revision ID: 0021_tpm_enrolment
Revises: 0020_witness_cosignatures
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_tpm_enrolment"
down_revision: Union[str, None] = "0020_witness_cosignatures"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "machine_tpm_enrolments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("machine_id", sa.Uuid(), sa.ForeignKey("provider_machines.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("ek_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("ek_cert_pem", sa.Text(), nullable=False),
        sa.Column("ak_public", sa.Text(), nullable=False),
        sa.Column("ak_name", sa.String(length=68), nullable=False),
        sa.Column("secret_sha256", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_machine_tpm_enrolments_machine_id", "machine_tpm_enrolments", ["machine_id"])
    op.create_index("ix_machine_tpm_enrolments_ek_fingerprint", "machine_tpm_enrolments", ["ek_fingerprint"])
    op.create_index("ix_machine_tpm_enrolments_ak_name", "machine_tpm_enrolments", ["ak_name"])


def downgrade() -> None:
    op.drop_table("machine_tpm_enrolments")
