"""Third-party vendor services, quotas and renewal reminders.

Seeds the nine services ComputeCoin actually depends on.

What is seeded: the service, what it is used for, its dashboard, and the
PUBLICLY DOCUMENTED free-tier ceiling. Those are facts about the vendor.

What is deliberately left NULL: ``cost_amount``, ``renewal_date`` and
``current_usage``. Those are facts about THIS account that nobody has told the
system, and inventing them would put fabricated financial data on an admin
dashboard. The UI shows "not set" and asks for them.

Revision ID: 0013_vendor_services
Revises: 0012_voucher_nonce
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_vendor_services"
down_revision: Union[str, None] = "0012_voucher_nonce"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (key, name, category, purpose, dashboard, plan, limit, unit, period, source,
#  billing_cycle, notes)
_SEED = [
    (
        "railway", "Railway", "hosting",
        "Backend API, scheduler and worker processes",
        "https://railway.app/dashboard", "Hobby", None, None, "month",
        "unavailable", "usage",
        "Usage-billed by compute-seconds, so there is no fixed ceiling to show a "
        "percentage of. The $5 monthly credit is consumed continuously — this is "
        "the service most likely to bill you without ever hitting a wall.",
    ),
    (
        "vercel", "Vercel", "hosting",
        "Next.js frontend hosting and CDN",
        "https://vercel.com/dashboard", "Hobby", 100, "GB bandwidth/month", "month",
        "unavailable", "free",
        "Hobby tier is non-commercial. Running a paid product on it breaches "
        "Vercel's terms — moving to Pro is a launch prerequisite, not an upgrade.",
    ),
    (
        "supabase_db", "Supabase Postgres", "database",
        "Primary database (users, jobs, ledger)",
        "https://supabase.com/dashboard", "Free", 500, "MB database", "none",
        "measured", "free",
        "Free projects are PAUSED after a week of inactivity. The ledger lives "
        "here, so this is the highest-consequence row in the table.",
    ),
    (
        "supabase_storage", "Supabase Storage", "storage",
        "Job artifacts, datasets and checkpoints",
        "https://supabase.com/dashboard", "Free", 1, "GB stored", "none",
        "measured", "free",
        "Grows with every completed job that uploads an artifact.",
    ),
    (
        "upstash", "Upstash Redis", "cache",
        "Rate limiting and shared counters",
        "https://console.upstash.com", "Free", 10000, "commands/day", "day",
        "measured", "free",
        "Every rate-limited request costs commands, so this scales with traffic "
        "rather than with customers. Exhausting it degrades rate limiting to the "
        "per-process fallback.",
    ),
    (
        "resend", "Resend", "email",
        "Email verification, password reset, spend alerts",
        "https://resend.com/overview", "Free", 3000, "emails/month", "month",
        "unavailable", "free",
        "Also capped at 100/day. Resend exposes no usage API, so this cannot be "
        "read automatically — count it ourselves or set it manually. If it runs "
        "out, nobody can verify an email address and signup silently breaks.",
    ),
    (
        "sentry", "Sentry", "monitoring",
        "Error and performance tracking",
        "https://sentry.io", "Developer", 5000, "errors/month", "month",
        "unavailable", "free",
        "NOT currently enabled in production (health reports sentry: disabled), "
        "so right now there is no error tracking on the live API at all.",
    ),
    (
        "chain_rpc", "Chain RPC provider", "blockchain",
        "On-chain settlement reconciliation",
        None, "Free", None, "requests/month", "month",
        "measured", "free",
        "Alchemy/Infura free tiers cap monthly requests. The reconciler chunks "
        "eth_getLogs, so request count scales with the block range scanned — set "
        "CHAIN_LOG_FROM_BLOCK to the deploy block to avoid rescanning history.",
    ),
    (
        "stripe", "Stripe", "payments",
        "Customer card payments and provider payouts (Connect)",
        "https://dashboard.stripe.com", "Standard", None, None, "none",
        "vendor_api", "per_transaction",
        "No quota — charged per transaction. Stripe DOES expose a usable API, so "
        "real figures can be pulled here once live keys are configured.",
    ),
]


def upgrade() -> None:
    op.create_table(
        "vendor_services",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=255), nullable=False),
        sa.Column("dashboard_url", sa.String(length=512), nullable=True),
        sa.Column("plan_name", sa.String(length=64), nullable=True),
        sa.Column("quota_limit", sa.Numeric(20, 4), nullable=True),
        sa.Column("quota_unit", sa.String(length=32), nullable=True),
        sa.Column("quota_period", sa.String(length=16), nullable=False, server_default="month"),
        sa.Column("current_usage", sa.Numeric(20, 4), nullable=True),
        sa.Column("usage_source", sa.String(length=16), nullable=False, server_default="unavailable"),
        sa.Column("usage_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cost_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("cost_currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("billing_cycle", sa.String(length=16), nullable=False, server_default="free"),
        sa.Column("renewal_date", sa.Date(), nullable=True),
        sa.Column("reminder_days_before", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("alert_thresholds", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_vendor_services_key"),
    )
    op.create_index("ix_vendor_services_key", "vendor_services", ["key"])

    op.create_table(
        "vendor_alerts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("alert_type", sa.String(length=32), nullable=False),
        # NOT NULL on purpose: SQL treats NULLs as distinct, so a nullable
        # column here would silently defeat uq_vendor_alert_once and the
        # same renewal reminder would re-insert on every sweep.
        sa.Column("threshold_pct", sa.Integer(), nullable=False, server_default="-1"),
        sa.Column("period_key", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("acknowledged", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        # This constraint, not application logic, is what makes an alert fire
        # exactly once per period under concurrent sweeps.
        sa.UniqueConstraint(
            "vendor_id", "alert_type", "threshold_pct", "period_key",
            name="uq_vendor_alert_once",
        ),
    )
    op.create_index("ix_vendor_alerts_vendor_id", "vendor_alerts", ["vendor_id"])
    op.create_index("ix_vendor_alerts_created_at", "vendor_alerts", ["created_at"])

    import uuid as _uuid

    rows = []
    for (key, name, cat, purpose, url, plan, limit, unit, period, source,
         cycle, notes) in _SEED:
        rows.append({
            "id": _uuid.uuid4(),
            "key": key, "name": name, "category": cat, "purpose": purpose,
            "dashboard_url": url, "plan_name": plan,
            "quota_limit": limit, "quota_unit": unit, "quota_period": period,
            "current_usage": None, "usage_source": source,
            "cost_amount": None, "cost_currency": "USD", "billing_cycle": cycle,
            "renewal_date": None, "reminder_days_before": 7,
            "alert_thresholds": None, "notes": notes, "active": True,
        })
    op.bulk_insert(sa.table(
        "vendor_services",
        sa.column("id", sa.Uuid()),
        sa.column("key", sa.String()),
        sa.column("name", sa.String()),
        sa.column("category", sa.String()),
        sa.column("purpose", sa.String()),
        sa.column("dashboard_url", sa.String()),
        sa.column("plan_name", sa.String()),
        sa.column("quota_limit", sa.Numeric()),
        sa.column("quota_unit", sa.String()),
        sa.column("quota_period", sa.String()),
        sa.column("current_usage", sa.Numeric()),
        sa.column("usage_source", sa.String()),
        sa.column("cost_amount", sa.Numeric()),
        sa.column("cost_currency", sa.String()),
        sa.column("billing_cycle", sa.String()),
        sa.column("renewal_date", sa.Date()),
        sa.column("reminder_days_before", sa.Integer()),
        sa.column("alert_thresholds", sa.String()),
        sa.column("notes", sa.Text()),
        sa.column("active", sa.Boolean()),
    ), rows)


def downgrade() -> None:
    op.drop_index("ix_vendor_alerts_created_at", table_name="vendor_alerts")
    op.drop_index("ix_vendor_alerts_vendor_id", table_name="vendor_alerts")
    op.drop_table("vendor_alerts")
    op.drop_index("ix_vendor_services_key", table_name="vendor_services")
    op.drop_table("vendor_services")
