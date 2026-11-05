"""U10 interactive sessions: compute_sessions.

Revision ID: 0033_sessions
Revises: 0032_user_aup
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033_sessions"
down_revision: Union[str, None] = "0032_user_aup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    def tz(name: str, nullable: bool = True) -> sa.Column:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)

    op.create_table(
        "compute_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("stop_reason", sa.String(32), nullable=True),
        sa.Column("kit_ref", sa.String(128), nullable=True),
        sa.Column("image_digest", sa.String(255), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("tcp_port", sa.Integer(), nullable=True),
        sa.Column("command", sa.JSON(), nullable=True),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("isolation_tier", sa.String(16), nullable=False),
        sa.Column("network_policy", sa.JSON(), nullable=True),
        sa.Column("max_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("idle_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_price_per_gpu_hour", sa.Numeric(18, 6), nullable=False),
        sa.Column("held_minor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("captured_minor", sa.Integer(), nullable=True),
        sa.Column("rate_per_gpu_hour", sa.Numeric(18, 6), nullable=True),
        sa.Column("active_seconds", sa.Integer(), nullable=True),
        sa.Column("machine_id", sa.Uuid(), nullable=True),
        sa.Column("provider_id", sa.Uuid(), nullable=True),
        sa.Column("client_pubkey", sa.String(64), nullable=False),
        sa.Column("host_pubkey", sa.String(64), nullable=True),
        sa.Column("host_token_hash", sa.String(64), nullable=True, unique=True),
        tz("host_token_expires_at"),
        tz("host_token_used_at"),
        sa.Column("browser_token_hash", sa.String(64), nullable=True, unique=True),
        tz("browser_token_expires_at"),
        tz("browser_token_used_at"),
        tz("started_at"),
        tz("last_host_heartbeat_at"),
        tz("last_activity_at"),
        tz("stopped_at"),
        tz("created_at", nullable=False),
        tz("updated_at", nullable=False),
    )
    op.create_index("ix_compute_sessions_status", "compute_sessions", ["status"])
    op.create_index("ix_compute_sessions_customer_status", "compute_sessions", ["customer_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_compute_sessions_customer_status", table_name="compute_sessions")
    op.drop_index("ix_compute_sessions_status", table_name="compute_sessions")
    op.drop_table("compute_sessions")
