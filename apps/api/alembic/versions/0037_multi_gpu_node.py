"""U13 multi-GPU / datacenter multi-node: datacenter_group on machines, pairwise network benchmarks, node groups.

Revision ID: 0037_multi_gpu_node
Revises: 0036_services
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037_multi_gpu_node"
down_revision: Union[str, None] = "0036_services"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("provider_machines", sa.Column("datacenter_group", sa.String(64), nullable=True))
    op.add_column("provider_machines", sa.Column("cluster_addr", sa.String(64), nullable=True))
    op.create_index("ix_provider_machines_datacenter_group", "provider_machines", ["datacenter_group"])
    op.create_table(
        "network_benchmarks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("from_machine_id", sa.Uuid(), sa.ForeignKey("provider_machines.id", ondelete="CASCADE"), nullable=False),
        sa.Column("to_machine_id", sa.Uuid(), sa.ForeignKey("provider_machines.id", ondelete="CASCADE"), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("bandwidth_mbps", sa.Float(), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("from_machine_id", "to_machine_id", name="uq_network_benchmarks_pair"),
    )
    op.create_index("ix_network_benchmarks_to", "network_benchmarks", ["to_machine_id"])
    op.create_table(
        "node_groups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("master_job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("datacenter_group", sa.String(64), nullable=False),
        sa.Column("node_count", sa.Integer(), nullable=False),
        sa.Column("master_port", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="placed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "node_group_members",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("group_id", sa.Uuid(), sa.ForeignKey("node_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("machine_id", sa.Uuid(), sa.ForeignKey("provider_machines.id", ondelete="CASCADE"), nullable=False),
        sa.UniqueConstraint("group_id", "rank", name="uq_node_group_members_rank"),
        sa.UniqueConstraint("job_id", name="uq_node_group_members_job"),
    )
    op.create_index("ix_node_group_members_group_id", "node_group_members", ["group_id"])
    op.create_index("ix_node_group_members_machine_id", "node_group_members", ["machine_id"])


def downgrade() -> None:
    op.drop_table("node_group_members")
    op.drop_table("node_groups")
    op.drop_index("ix_network_benchmarks_to", table_name="network_benchmarks")
    op.drop_table("network_benchmarks")
    op.drop_index("ix_provider_machines_datacenter_group", table_name="provider_machines")
    op.drop_column("provider_machines", "cluster_addr")
    op.drop_column("provider_machines", "datacenter_group")
