"""phases B/C/D: verification, reputation & slashing, spend controls, webhooks,
enterprise (invitations/SSO/Connect), batches/datasets/checkpoints, and chain
reconciliation runs.

Revision ID: 0011_phases_bcd
Revises: 0010_stripe_billing_phase5
Create Date: 2026-10-23
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_phases_bcd"
down_revision: Union[str, None] = "0010_stripe_billing_phase5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FALSE = sa.text("false")
_TRUE = sa.text("true")


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Phase C — batches first: jobs.batch_id references it below.
    # ------------------------------------------------------------------
    op.create_table(
        "batches",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="created"),
        sa.Column("total_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discount_pct", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "customer_id", "idempotency_key", name="uq_batches_customer_idempotency_key"
        ),
    )
    op.create_index("ix_batches_customer_status", "batches", ["customer_id", "status"])

    # ------------------------------------------------------------------
    # Phase B — verification
    # ------------------------------------------------------------------
    op.create_table(
        "verification_challenges",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("workload_type", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False, index=True),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("expected_output_hash", sa.String(length=64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=_TRUE),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_verification_challenges_workload_active",
        "verification_challenges",
        ["workload_type", "active"],
    )

    op.create_table(
        "verification_results",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "challenge_id",
            sa.Uuid(),
            sa.ForeignKey("verification_challenges.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=_FALSE),
        sa.Column("expected_hash", sa.String(length=64), nullable=True),
        sa.Column("actual_hash", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )
    op.create_index(
        "ix_verification_results_job_kind", "verification_results", ["job_id", "kind"]
    )

    op.create_table(
        "redundancy_checks",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "primary_job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "shadow_job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("matched", sa.Boolean(), nullable=True),
        sa.Column("similarity", sa.Numeric(6, 4), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_redundancy_checks_status_created", "redundancy_checks", ["status", "created_at"]
    )

    # ------------------------------------------------------------------
    # Phase B — reputation, slashing, dispute evidence
    # ------------------------------------------------------------------
    op.create_table(
        "provider_reputation",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "provider_id",
            sa.Uuid(),
            sa.ForeignKey("providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "machine_id",
            sa.Uuid(),
            sa.ForeignKey("provider_machines.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("score", sa.Numeric(6, 4), nullable=False, server_default="0.5"),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verification_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("disputes_lost", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decayed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider_id", name="uq_provider_reputation_provider_id"),
    )

    op.create_table(
        "reputation_events",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "provider_id",
            sa.Uuid(),
            sa.ForeignKey("providers.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "machine_id",
            sa.Uuid(),
            sa.ForeignKey("provider_machines.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False, index=True),
        sa.Column("delta", sa.Numeric(6, 4), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(length=512), nullable=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_reputation_events_provider_created",
        "reputation_events",
        ["provider_id", "created_at"],
    )

    op.create_table(
        "slash_events",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "provider_id",
            sa.Uuid(),
            sa.ForeignKey("providers.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=32), nullable=False, server_default="USDC_CREDITS"),
        sa.Column("reason", sa.String(length=1024), nullable=False),
        sa.Column(
            "ledger_txn_id",
            sa.Uuid(),
            sa.ForeignKey("ledger_transactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "actor_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_slash_events_provider_created", "slash_events", ["provider_id", "created_at"]
    )

    op.create_table(
        "dispute_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "dispute_id",
            sa.Uuid(),
            sa.ForeignKey("disputes.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "submitted_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_dispute_evidence_dispute_kind", "dispute_evidence", ["dispute_id", "kind"]
    )

    # ------------------------------------------------------------------
    # Phase B — spend limits + idempotent threshold alerts
    # ------------------------------------------------------------------
    op.create_table(
        "spend_limits",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("period", sa.String(length=32), nullable=False),
        sa.Column("limit_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=32), nullable=False, server_default="USDC_CREDITS"),
        sa.Column("alert_thresholds", sa.JSON(), nullable=False),
        sa.Column("enforce", sa.Boolean(), nullable=False, server_default=_TRUE),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=_TRUE),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "scope", "scope_id", "period", name="uq_spend_limits_scope_scope_id_period"
        ),
    )
    op.create_index("ix_spend_limits_scope_scope_id", "spend_limits", ["scope", "scope_id"])

    op.create_table(
        "spend_alerts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "spend_limit_id",
            sa.Uuid(),
            sa.ForeignKey("spend_limits.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("threshold_pct", sa.Integer(), nullable=False),
        sa.Column("period_key", sa.String(length=32), nullable=False),
        sa.Column("spent_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("limit_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "spend_limit_id",
            "threshold_pct",
            "period_key",
            name="uq_spend_alerts_limit_threshold_period",
        ),
    )

    # ------------------------------------------------------------------
    # Phase B — webhooks
    # ------------------------------------------------------------------
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("secret_hash", sa.String(length=128), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=_TRUE),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_webhook_endpoints_customer_active", "webhook_endpoints", ["customer_id", "active"]
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "endpoint_id",
            sa.Uuid(),
            sa.ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False, index=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("delivery_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("delivery_key", name="uq_webhook_deliveries_delivery_key"),
    )
    op.create_index(
        "ix_webhook_deliveries_status_next_attempt",
        "webhook_deliveries",
        ["status", "next_attempt_at"],
    )

    # ------------------------------------------------------------------
    # Phase C — enterprise
    # ------------------------------------------------------------------
    op.create_table(
        "organization_invitations",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False, server_default="customer"),
        sa.Column("token_hash", sa.String(length=128), nullable=False, index=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "accepted_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "invited_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("token_hash", name="uq_org_invitations_token_hash"),
    )
    op.create_index(
        "ix_org_invitations_org_email", "organization_invitations", ["org_id", "email"]
    )

    op.create_table(
        "sso_connections",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=_TRUE),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "org_id", "provider", "issuer", name="uq_sso_connections_org_provider_issuer"
        ),
    )
    op.create_index("ix_sso_connections_issuer_active", "sso_connections", ["issuer", "active"])

    op.create_table(
        "connect_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "provider_id",
            sa.Uuid(),
            sa.ForeignKey("providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stripe_account_id", sa.String(length=255), nullable=False, index=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("payouts_enabled", sa.Boolean(), nullable=False, server_default=_FALSE),
        sa.Column("details_submitted", sa.Boolean(), nullable=False, server_default=_FALSE),
        sa.Column("requirements", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider_id", name="uq_connect_accounts_provider_id"),
        sa.UniqueConstraint("stripe_account_id", name="uq_connect_accounts_stripe_account_id"),
    )

    # ------------------------------------------------------------------
    # Phase C — datasets & training checkpoints
    # ------------------------------------------------------------------
    op.create_table(
        "datasets",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(length=64), nullable=False, index=True),
        sa.Column(
            "content_type",
            sa.String(length=128),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="uploaded"),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_datasets_customer_status", "datasets", ["customer_id", "status"])

    op.create_table(
        "training_checkpoints",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("job_id", "step", name="uq_training_checkpoints_job_step"),
    )

    # ------------------------------------------------------------------
    # Phase D — chain reconciliation audit runs
    # ------------------------------------------------------------------
    op.create_table(
        "chain_reconciliation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escrows_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("drift_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=_FALSE),
    )
    op.create_index(
        "ix_chain_reconciliation_runs_chain_started",
        "chain_reconciliation_runs",
        ["chain_id", "started_at"],
    )

    # ------------------------------------------------------------------
    # ALTERs on existing tables. All new columns are nullable or carry a
    # server default, so rows written before this migration stay valid.
    # batch_alter_table keeps SQLite (tests) working, where ADD COLUMN cannot
    # introduce a foreign key.
    # ------------------------------------------------------------------
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("batch_id", sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column("is_challenge", sa.Boolean(), nullable=False, server_default=_FALSE)
        )
        batch.add_column(sa.Column("shadow_of_job_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_jobs_batch_id_batches", "batches", ["batch_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_foreign_key(
            "fk_jobs_shadow_of_job_id_jobs", "jobs", ["shadow_of_job_id"], ["id"], ondelete="SET NULL"
        )
    op.create_index("ix_jobs_batch_id", "jobs", ["batch_id"])
    op.create_index("ix_jobs_shadow_of_job_id", "jobs", ["shadow_of_job_id"])

    with op.batch_alter_table("provider_machines") as batch:
        batch.add_column(sa.Column("floor_price_per_hour", sa.Numeric(12, 2), nullable=True))
        batch.add_column(sa.Column("availability_windows", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("model_allowlist", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("provider_machines") as batch:
        batch.drop_column("model_allowlist")
        batch.drop_column("availability_windows")
        batch.drop_column("floor_price_per_hour")

    op.drop_index("ix_jobs_shadow_of_job_id", table_name="jobs")
    op.drop_index("ix_jobs_batch_id", table_name="jobs")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("fk_jobs_shadow_of_job_id_jobs", type_="foreignkey")
        batch.drop_constraint("fk_jobs_batch_id_batches", type_="foreignkey")
        batch.drop_column("shadow_of_job_id")
        batch.drop_column("is_challenge")
        batch.drop_column("batch_id")

    op.drop_index(
        "ix_chain_reconciliation_runs_chain_started", table_name="chain_reconciliation_runs"
    )
    op.drop_table("chain_reconciliation_runs")

    op.drop_table("training_checkpoints")
    op.drop_index("ix_datasets_customer_status", table_name="datasets")
    op.drop_table("datasets")

    op.drop_table("connect_accounts")
    op.drop_index("ix_sso_connections_issuer_active", table_name="sso_connections")
    op.drop_table("sso_connections")
    op.drop_index("ix_org_invitations_org_email", table_name="organization_invitations")
    op.drop_table("organization_invitations")

    op.drop_index("ix_webhook_deliveries_status_next_attempt", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_webhook_endpoints_customer_active", table_name="webhook_endpoints")
    op.drop_table("webhook_endpoints")

    op.drop_table("spend_alerts")
    op.drop_index("ix_spend_limits_scope_scope_id", table_name="spend_limits")
    op.drop_table("spend_limits")

    op.drop_index("ix_dispute_evidence_dispute_kind", table_name="dispute_evidence")
    op.drop_table("dispute_evidence")
    op.drop_index("ix_slash_events_provider_created", table_name="slash_events")
    op.drop_table("slash_events")
    op.drop_index("ix_reputation_events_provider_created", table_name="reputation_events")
    op.drop_table("reputation_events")
    op.drop_table("provider_reputation")

    op.drop_index("ix_redundancy_checks_status_created", table_name="redundancy_checks")
    op.drop_table("redundancy_checks")
    op.drop_index("ix_verification_results_job_kind", table_name="verification_results")
    op.drop_table("verification_results")
    op.drop_index(
        "ix_verification_challenges_workload_active", table_name="verification_challenges"
    )
    op.drop_table("verification_challenges")

    op.drop_index("ix_batches_customer_status", table_name="batches")
    op.drop_table("batches")
