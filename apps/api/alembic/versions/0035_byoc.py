"""U8 BYOC: image denylist and scan cache (new tables only, additive).

Revision ID: 0035_byoc
Revises: 0034_pipelines
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035_byoc"
down_revision: Union[str, None] = "0034_pipelines"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "byoc_image_denylist",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("digest", sa.String(71), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("added_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_byoc_image_denylist_digest", "byoc_image_denylist", ["digest"], unique=True)
    op.create_table(
        "byoc_image_scans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("digest", sa.String(71), nullable=False),
        sa.Column("scanner", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=True),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_byoc_image_scans_digest", "byoc_image_scans", ["digest"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_byoc_image_scans_digest", table_name="byoc_image_scans")
    op.drop_table("byoc_image_scans")
    op.drop_index("ix_byoc_image_denylist_digest", table_name="byoc_image_denylist")
    op.drop_table("byoc_image_denylist")
