"""phase 4 job execution: model registry, jobs, FSM events, attempts, logs, artifacts

Revision ID: 0004_jobs_phase4
Revises: 0003_marketplace_phase3
Create Date: 2026-10-17

Seeds one approved simulated model (``echo-sim``) and version ``echo-sim-v1``
(workloads [text-generation, batch-inference], runtime "simulated", trust_status
"approved") so the submit pipeline has an approved model to schedule against.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_jobs_phase4"
down_revision: Union[str, None] = "0003_marketplace_phase3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Deterministic ids for the seeded model + version.
_MODEL_ID = uuid.UUID("2f3a6c1e-0d47-4e9a-9d2b-7b1f0a5c4e88")
_MODEL_VERSION_ID = uuid.UUID("a1c9b7d4-2e6f-4a13-8c5d-9f0e1b2a3c4d")


def upgrade() -> None:
    models = op.create_table(
        "models",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("trust_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_models_name", "models", ["name"], unique=True)

    model_versions = op.create_table(
        "model_versions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("version_tag", sa.String(length=128), nullable=False),
        sa.Column("runtime", sa.String(length=32), nullable=False),
        sa.Column("image_digest", sa.String(length=255), nullable=True),
        sa.Column("supported_workloads", sa.JSON(), nullable=False),
        sa.Column("trust_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("version_tag", name="uq_model_versions_version_tag"),
    )
    op.create_index("ix_model_versions_model_id", "model_versions", ["model_id"])
    op.create_index("ix_model_versions_version_tag", "model_versions", ["version_tag"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("workload_type", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("machine_id", sa.Uuid(), nullable=True),
        sa.Column("reservation_id", sa.Uuid(), nullable=True),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("estimated_minutes", sa.Integer(), nullable=False),
        sa.Column("max_cost", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("max_runtime_seconds", sa.Integer(), nullable=False),
        sa.Column("retry_policy", sa.JSON(), nullable=False),
        sa.Column("attempts_made", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("execution_mode", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("match_explanation", sa.JSON(), nullable=True),
        sa.Column("verification", sa.JSON(), nullable=True),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("final_cost", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["machine_id"], ["provider_machines.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["resource_reservations.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "customer_id", "idempotency_key", name="uq_jobs_customer_idempotency_key"
        ),
    )
    op.create_index("ix_jobs_customer_id", "jobs", ["customer_id"])
    op.create_index("ix_jobs_project_id", "jobs", ["project_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_priority", "jobs", ["priority"])
    op.create_index("ix_jobs_machine_id", "jobs", ["machine_id"])

    op.create_table(
        "job_events",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("actor", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("job_id", "seq", name="uq_job_events_job_id_seq"),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])

    op.create_table(
        "job_assignments",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("machine_id", sa.Uuid(), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["machine_id"], ["provider_machines.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["resource_reservations.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_job_assignments_job_id", "job_assignments", ["job_id"])
    op.create_index("ix_job_assignments_machine_id", "job_assignments", ["machine_id"])

    op.create_table(
        "job_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("machine_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["machine_id"], ["provider_machines.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_job_attempts_job_id", "job_attempts", ["job_id"])

    op.create_table(
        "job_logs_metadata",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_job_logs_metadata_job_id", "job_logs_metadata", ["job_id"])

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_artifacts_job_id", "artifacts", ["job_id"])

    # Seed one approved simulated model + version.
    now = dt.datetime.now(dt.timezone.utc)
    op.bulk_insert(
        models,
        [
            {
                "id": _MODEL_ID,
                "name": "echo-sim",
                "display_name": "Echo (Simulated)",
                "trust_status": "approved",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    op.bulk_insert(
        model_versions,
        [
            {
                "id": _MODEL_VERSION_ID,
                "model_id": _MODEL_ID,
                "version_tag": "echo-sim-v1",
                "runtime": "simulated",
                "image_digest": None,
                "supported_workloads": ["text-generation", "batch-inference"],
                "trust_status": "approved",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_artifacts_job_id", table_name="artifacts")
    op.drop_table("artifacts")

    op.drop_index("ix_job_logs_metadata_job_id", table_name="job_logs_metadata")
    op.drop_table("job_logs_metadata")

    op.drop_index("ix_job_attempts_job_id", table_name="job_attempts")
    op.drop_table("job_attempts")

    op.drop_index("ix_job_assignments_machine_id", table_name="job_assignments")
    op.drop_index("ix_job_assignments_job_id", table_name="job_assignments")
    op.drop_table("job_assignments")

    op.drop_index("ix_job_events_job_id", table_name="job_events")
    op.drop_table("job_events")

    op.drop_index("ix_jobs_machine_id", table_name="jobs")
    op.drop_index("ix_jobs_priority", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_project_id", table_name="jobs")
    op.drop_index("ix_jobs_customer_id", table_name="jobs")
    op.drop_table("jobs")

    op.drop_index("ix_model_versions_version_tag", table_name="model_versions")
    op.drop_index("ix_model_versions_model_id", table_name="model_versions")
    op.drop_table("model_versions")

    op.drop_index("ix_models_name", table_name="models")
    op.drop_table("models")
