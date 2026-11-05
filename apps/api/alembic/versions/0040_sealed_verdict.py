"""A6: persist the machine-level Sealed attestation verdict on provider_machines.

Revision ID: 0040_sealed_verdict
Revises: 0037_multi_gpu_node
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0040_sealed_verdict"
down_revision: Union[str, None] = "0037_multi_gpu_node"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("provider_machines", sa.Column("sealed_verdict", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("provider_machines", "sealed_verdict")
