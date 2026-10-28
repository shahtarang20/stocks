"""Store log witness cosignatures on the last leaf of each witnessed checkpoint (WP-E).

Revision ID: 0020_witness_cosignatures
Revises: 0019_output_commitment
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_witness_cosignatures"
down_revision: Union[str, None] = "0019_output_commitment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("transparency_log") as batch_op:
        batch_op.add_column(sa.Column("witness_cosignatures", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("transparency_log") as batch_op:
        batch_op.drop_column("witness_cosignatures")
