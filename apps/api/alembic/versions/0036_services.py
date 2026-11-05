"""Services / endpoints tables (U11): services, service_replicas, service_requests. No existing table is changed.

Revision ID: 0036_services
Revises: 0035_byoc
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036_services"
down_revision: Union[str, None] = "0035_byoc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "services",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("kit_ref", sa.String(128), nullable=False),
        sa.Column("isolation_tier", sa.String(32), nullable=False),
        sa.Column("required_gpu_runtime", sa.String(32), nullable=False),
        sa.Column("min_replicas", sa.Integer(), nullable=False),
        sa.Column("max_replicas", sa.Integer(), nullable=False),
        sa.Column("scale_to_zero", sa.Boolean(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("health_path", sa.String(128), nullable=False),
        sa.Column("request_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_total_cost", sa.Numeric(14, 2), nullable=False),
        sa.Column("desired_replicas", sa.Integer(), nullable=False),
        sa.Column("capped", sa.Boolean(), nullable=False),
        sa.Column("last_request_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scaled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_services_customer_id", "services", ["customer_id"])
    op.create_index("ix_services_customer_status", "services", ["customer_id", "status"])

    op.create_table(
        "service_replicas",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("machine_id", sa.Uuid(), sa.ForeignKey("provider_machines.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("window_ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorised_cost", sa.Numeric(14, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("healthy_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(48), nullable=True),
        sa.Column("in_flight", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_service_replicas_service_status", "service_replicas", ["service_id", "status"])

    op.create_table(
        "service_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("replica_id", sa.Uuid(), sa.ForeignKey("service_replicas.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(256), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("body_b64", sa.Text(), nullable=False),
        sa.Column("no_retry", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_content_type", sa.String(128), nullable=True),
        sa.Column("response_b64", sa.Text(), nullable=True),
        sa.Column("response_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("service_id", "idempotency_key", name="uq_service_requests_idem"),
    )
    op.create_index("ix_service_requests_service_status", "service_requests", ["service_id", "status", "queued_at"])


def downgrade() -> None:
    op.drop_table("service_requests")
    op.drop_table("service_replicas")
    op.drop_table("services")
