"""Display name, and a 'deleted' account state.

Account deletion anonymises the user row (email, name, password) instead of removing
it, because jobs, invoices and the double-entry ledger reference users and must stay
intact and balanced.

Revision ID: 0015_profile_deletion
Revises: 0014_project_description
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_profile_deletion"
down_revision: Union[str, None] = "0014_project_description"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("display_name", sa.String(length=120), nullable=True))
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        # ALTER TYPE ... ADD VALUE cannot run inside the migration transaction.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE user_status ADD VALUE IF NOT EXISTS 'deleted'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; the 'deleted' label is left in place.
    op.drop_column("users", "deleted_at")
    op.drop_column("users", "display_name")
