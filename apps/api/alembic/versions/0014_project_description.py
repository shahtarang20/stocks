"""Projects get a description.

The console's "New project" dialog has a Description field and the web client
sends it, but the API had no column for it: the value was accepted and silently
discarded.

Revision ID: 0014_project_description
Revises: 0013_vendor_services
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_project_description"
down_revision: Union[str, None] = "0013_vendor_services"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("description", sa.String(length=1000), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "description")
