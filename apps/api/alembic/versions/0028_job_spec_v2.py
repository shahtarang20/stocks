"""Job Spec v2 columns on jobs (U7): spec_v2 JSON, kit_id (slug), kit_version. All nullable; v1 rows unchanged.

Revision ID: 0028_job_spec_v2
Revises: 0027_data_plane
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_job_spec_v2"
down_revision: Union[str, None] = "0027_data_plane"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("spec_v2", sa.JSON(), nullable=True))
    op.add_column("jobs", sa.Column("kit_id", sa.String(64), nullable=True))
    op.add_column("jobs", sa.Column("kit_version", sa.String(32), nullable=True))
    op.create_index("ix_jobs_kit_id", "jobs", ["kit_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_kit_id", table_name="jobs")
    op.drop_column("jobs", "kit_version")
    op.drop_column("jobs", "kit_id")
    op.drop_column("jobs", "spec_v2")
