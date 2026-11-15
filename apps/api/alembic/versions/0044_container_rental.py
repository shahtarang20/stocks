"""Container rental columns on compute_sessions (docs/architecture/RENTAL-container-rental-design.md, section 6).

Adds: rental, template, requested_machine_id (+ index), workspace_gb, memory_mb, cpus, inputs, terms_accepted_at,
export_requested_at, export_state, exports. Existing rows are ordinary (Kit) sessions: rental false, workspace 0.

Revision ID: 0044_container_rental
Revises: 0043_unique_key_constraints
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0044_container_rental"
down_revision: Union[str, None] = "0043_unique_key_constraints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "compute_sessions"


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    have = _columns()
    adds = [
        sa.Column("rental", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("template", sa.String(length=64), nullable=True),
        sa.Column("requested_machine_id", sa.Uuid(), nullable=True),
        sa.Column("workspace_gb", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("memory_mb", sa.Integer(), nullable=True),
        sa.Column("cpus", sa.Integer(), nullable=True),
        sa.Column("inputs", sa.JSON(), nullable=True),
        sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("export_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("export_state", sa.String(length=16), nullable=True),
        sa.Column("exports", sa.JSON(), nullable=True),
    ]
    for col in adds:
        if col.name not in have:
            op.add_column(TABLE, col)
    indexes = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(TABLE)}
    if "ix_compute_sessions_requested_machine_id" not in indexes:
        op.create_index("ix_compute_sessions_requested_machine_id", TABLE, ["requested_machine_id"])


def downgrade() -> None:
    indexes = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(TABLE)}
    if "ix_compute_sessions_requested_machine_id" in indexes:
        op.drop_index("ix_compute_sessions_requested_machine_id", table_name=TABLE)
    have = _columns()
    with op.batch_alter_table(TABLE) as batch:
        for name in ("exports", "export_state", "export_requested_at", "terms_accepted_at", "inputs", "cpus", "memory_mb",
                     "workspace_gb", "requested_machine_id", "template", "rental"):
            if name in have:
                batch.drop_column(name)
