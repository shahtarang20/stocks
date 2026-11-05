"""Per-job network allowlist: jobs.network_policy (B5).

Revision ID: 0024_job_network_policy
Revises: 0023_stake_sync
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_job_network_policy"
down_revision: Union[str, None] = "0023_stake_sync"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("network_policy", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("network_policy")
