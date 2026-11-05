"""Workload Kit registry (U1): the kits table.

Revision ID: 0026_kits
Revises: 0025_withdrawals
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026_kits"
down_revision: Union[str, None] = "0025_withdrawals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("image_digest", sa.String(255), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(128), nullable=False),
        sa.Column("signing_key_id", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="published"),
        sa.Column("published_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("yanked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("slug", "version", name="uq_kits_slug_version"),
    )
    op.create_index("ix_kits_category", "kits", ["category"])


def downgrade() -> None:
    op.drop_index("ix_kits_category", table_name="kits")
    op.drop_table("kits")
