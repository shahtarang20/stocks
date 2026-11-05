"""U16 AUP acceptance record on users (nullable, additive).

Revision ID: 0032_user_aup
Revises: 0031_abuse
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032_user_aup"
down_revision: Union[str, None] = "0031_abuse"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("aup_version", sa.String(32), nullable=True))
    op.add_column("users", sa.Column("aup_accepted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "aup_accepted_at")
    op.drop_column("users", "aup_version")
