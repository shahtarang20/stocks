"""Add output_commitment and guardian_releases columns to jobs.

Revision ID: 0019_output_commitment
Revises: 0018_proof_of_privacy
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_output_commitment"
down_revision: Union[str, None] = "0018_proof_of_privacy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("output_commitment", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("guardian_releases", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("guardian_releases")
        batch_op.drop_column("output_commitment")
