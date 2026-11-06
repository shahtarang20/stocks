"""W6 agent release management: signed update manifests per platform and ring, and per-machine ring assignment.

Revision ID: 0042_agent_releases
Revises: 0041_service_request_charges
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0042_agent_releases"
down_revision: Union[str, None] = "0041_service_request_charges"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_releases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("ring", sa.String(16), nullable=False),
        sa.Column("min_supported", sa.String(40), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(128), nullable=False),
        sa.Column("signing_key_id", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_reason", sa.String(255), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_changed_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("published_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("platform", "version", "ring", name="uq_agent_releases_platform_version_ring"),
    )
    op.create_index("ix_agent_releases_platform_status", "agent_releases", ["platform", "status"])
    op.create_table(
        "agent_ring_assignments",
        sa.Column("machine_id", sa.Uuid(), sa.ForeignKey("provider_machines.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("ring", sa.String(16), nullable=False),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("agent_ring_assignments")
    op.drop_index("ix_agent_releases_platform_status", table_name="agent_releases")
    op.drop_table("agent_releases")
