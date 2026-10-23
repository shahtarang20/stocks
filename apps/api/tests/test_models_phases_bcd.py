"""Foundation tests for the Phase B/C/D schema.

These assert the things later phases *rely on* rather than merely that the
tables exist: the UNIQUE constraints that make alerting and webhook delivery
idempotent, the ownership/ FK wiring, the new ``jobs`` / ``provider_machines``
columns, and that money/score columns are integer-or-Decimal (never float).
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.db.session import get_sessionmaker
from app.models import (
    Batch,
    ChainReconciliationRun,
    ConnectAccount,
    Dataset,
    DisputeEvidence,
    Job,
    OrganizationInvitation,
    ProviderReputation,
    RedundancyCheck,
    ReputationEvent,
    SlashEvent,
    SpendAlert,
    SpendLimit,
    SsoConnection,
    TrainingCheckpoint,
    VerificationChallenge,
    VerificationResult,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.models.org import Organization
from app.models.provider import Provider

from tests.helpers import get_user_id, register_verify_login

NEW_TABLES = [
    "verification_challenges",
    "verification_results",
    "redundancy_checks",
    "provider_reputation",
    "reputation_events",
    "slash_events",
    "dispute_evidence",
    "spend_limits",
    "spend_alerts",
    "webhook_endpoints",
    "webhook_deliveries",
    "organization_invitations",
    "sso_connections",
    "batches",
    "datasets",
    "training_checkpoints",
    "connect_accounts",
    "chain_reconciliation_runs",
]


async def _make_user(client, email: str) -> uuid.UUID:
    await register_verify_login(client, email)
    return await get_user_id(email)


async def _make_provider(user_id: uuid.UUID) -> uuid.UUID:
    sm = get_sessionmaker()
    async with sm() as s:
        provider = Provider(user_id=user_id, display_name="p")
        s.add(provider)
        await s.commit()
        return provider.id


async def test_all_phase_bcd_tables_exist(db_engine) -> None:
    async with db_engine.connect() as conn:
        names = await conn.run_sync(lambda c: sa.inspect(c).get_table_names())
    missing = [t for t in NEW_TABLES if t not in names]
    assert missing == [], f"missing tables: {missing}"


async def test_job_and_machine_gain_phase_bcd_columns(db_engine) -> None:
    async with db_engine.connect() as conn:
        job_cols = await conn.run_sync(
            lambda c: {col["name"] for col in sa.inspect(c).get_columns("jobs")}
        )
        machine_cols = await conn.run_sync(
            lambda c: {col["name"] for col in sa.inspect(c).get_columns("provider_machines")}
        )
    assert {"batch_id", "is_challenge", "shadow_of_job_id"} <= job_cols
    assert {
        "floor_price_per_hour",
        "availability_windows",
        "model_allowlist",
    } <= machine_cols


async def test_job_defaults_are_not_challenge_and_unbatched(client) -> None:
    """A plain job must never be flagged as a verification challenge."""
    customer_id = await _make_user(client, "bcd-job@example.com")
    sm = get_sessionmaker()
    async with sm() as s:
        job = Job(customer_id=customer_id, workload_type="inference", model="echo-sim-v1")
        s.add(job)
        await s.commit()
        job_id = job.id

    async with sm() as s:
        loaded = await s.get(Job, job_id)
        assert loaded is not None
        assert loaded.is_challenge is False
        assert loaded.batch_id is None
        assert loaded.shadow_of_job_id is None


async def test_batch_links_jobs_and_dedupes_on_idempotency_key(client) -> None:
    customer_id = await _make_user(client, "bcd-batch@example.com")
    sm = get_sessionmaker()
    async with sm() as s:
        batch = Batch(
            customer_id=customer_id,
            total_items=2,
            discount_pct=decimal.Decimal("10.00"),
            idempotency_key="batch-key-1",
        )
        s.add(batch)
        await s.commit()
        batch_id = batch.id
        # Discount stays Decimal — money math is never float.
        assert isinstance(batch.discount_pct, decimal.Decimal)

    async with sm() as s:
        s.add(Batch(customer_id=customer_id, idempotency_key="batch-key-1"))
        with pytest.raises(IntegrityError):
            await s.commit()

    async with sm() as s:
        job = Job(
            customer_id=customer_id,
            workload_type="inference",
            model="echo-sim-v1",
            batch_id=batch_id,
        )
        s.add(job)
        await s.commit()
        assert job.batch_id == batch_id


async def test_shadow_job_references_primary_for_redundancy(client) -> None:
    customer_id = await _make_user(client, "bcd-shadow@example.com")
    sm = get_sessionmaker()
    async with sm() as s:
        primary = Job(customer_id=customer_id, workload_type="inference", model="echo-sim-v1")
        s.add(primary)
        await s.flush()
        shadow = Job(
            customer_id=customer_id,
            workload_type="inference",
            model="echo-sim-v1",
            shadow_of_job_id=primary.id,
        )
        s.add(shadow)
        await s.flush()
        check = RedundancyCheck(primary_job_id=primary.id, shadow_job_id=shadow.id)
        s.add(check)
        await s.commit()
        check_id, primary_id = check.id, primary.id

    async with sm() as s:
        loaded = await s.get(RedundancyCheck, check_id)
        assert loaded is not None
        assert loaded.primary_job_id == primary_id
        assert loaded.status == "pending"
        # NULL (not False) until the shadow resolves — "unknown" != "mismatch".
        assert loaded.matched is None
        assert loaded.similarity is None
        assert loaded.resolved_at is None


async def test_verification_result_records_hash_mismatch(client) -> None:
    customer_id = await _make_user(client, "bcd-verify@example.com")
    sm = get_sessionmaker()
    async with sm() as s:
        challenge = VerificationChallenge(
            workload_type="inference",
            model="echo-sim-v1",
            input={"prompt": "2+2"},
            expected_output_hash="a" * 64,
            config={"mode": "exact"},
        )
        job = Job(
            customer_id=customer_id,
            workload_type="inference",
            model="echo-sim-v1",
            is_challenge=True,
        )
        s.add_all([challenge, job])
        await s.flush()
        s.add(
            VerificationResult(
                job_id=job.id,
                challenge_id=challenge.id,
                kind="challenge",
                passed=False,
                expected_hash="a" * 64,
                actual_hash="b" * 64,
                detail={"reason": "hash_mismatch"},
            )
        )
        await s.commit()
        job_id = job.id

    async with sm() as s:
        row = (
            await s.execute(
                sa.select(VerificationResult).where(VerificationResult.job_id == job_id)
            )
        ).scalar_one()
        assert row.kind == "challenge"
        assert row.passed is False
        assert row.expected_hash != row.actual_hash
        assert ensure_aware(row.created_at) <= utcnow()
        assert challenge.active is True


async def test_reputation_aggregate_is_unique_per_provider_and_decimal(client) -> None:
    user_id = await _make_user(client, "bcd-rep@example.com")
    provider_id = await _make_provider(user_id)

    sm = get_sessionmaker()
    async with sm() as s:
        s.add(ProviderReputation(provider_id=provider_id))
        await s.commit()

    async with sm() as s:
        rep = (
            await s.execute(
                sa.select(ProviderReputation).where(
                    ProviderReputation.provider_id == provider_id
                )
            )
        ).scalar_one()
        # Neutral start, stored as Decimal (never float) so scoring is exact.
        assert isinstance(rep.score, decimal.Decimal)
        assert rep.score == decimal.Decimal("0.5")
        assert rep.completed_count == rep.failed_count == 0

    async with sm() as s:
        s.add(ProviderReputation(provider_id=provider_id))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_reputation_events_and_slash_use_minor_units(client) -> None:
    user_id = await _make_user(client, "bcd-slash@example.com")
    provider_id = await _make_provider(user_id)

    sm = get_sessionmaker()
    async with sm() as s:
        s.add(
            ReputationEvent(
                provider_id=provider_id,
                kind="verification_failed",
                delta=decimal.Decimal("-0.2000"),
                reason="challenge hash mismatch",
            )
        )
        s.add(
            SlashEvent(
                provider_id=provider_id,
                amount_minor=2500,
                reason="failed known-answer challenge",
                actor_user_id=user_id,
            )
        )
        await s.commit()

    async with sm() as s:
        event = (
            await s.execute(
                sa.select(ReputationEvent).where(ReputationEvent.provider_id == provider_id)
            )
        ).scalar_one()
        slash = (
            await s.execute(
                sa.select(SlashEvent).where(SlashEvent.provider_id == provider_id)
            )
        ).scalar_one()
        assert isinstance(event.delta, decimal.Decimal)
        assert event.delta < 0
        assert isinstance(slash.amount_minor, int)
        # Not yet posted to the ledger -> must not be read as a completed slash.
        assert slash.ledger_txn_id is None


async def test_spend_limit_unique_per_scope_and_alert_fires_once(client) -> None:
    owner_id = await _make_user(client, "bcd-spend@example.com")
    sm = get_sessionmaker()
    async with sm() as s:
        limit = SpendLimit(
            scope="customer",
            scope_id=owner_id,
            owner_id=owner_id,
            period="monthly",
            limit_minor=500_00,
            alert_thresholds=[50, 80, 100],
        )
        s.add(limit)
        await s.commit()
        limit_id = limit.id

    # Same (scope, scope_id, period) may exist only once.
    async with sm() as s:
        s.add(
            SpendLimit(
                scope="customer",
                scope_id=owner_id,
                owner_id=owner_id,
                period="monthly",
                limit_minor=999_00,
                alert_thresholds=[],
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()

    # Alerting is idempotent per (limit, threshold, period bucket)...
    async with sm() as s:
        s.add(
            SpendAlert(
                spend_limit_id=limit_id,
                threshold_pct=80,
                period_key="2026-10",
                spent_minor=400_00,
                limit_minor=500_00,
            )
        )
        await s.commit()

    async with sm() as s:
        s.add(SpendAlert(spend_limit_id=limit_id, threshold_pct=80, period_key="2026-10"))
        with pytest.raises(IntegrityError):
            await s.commit()

    # ...but the next period re-arms it.
    async with sm() as s:
        s.add(SpendAlert(spend_limit_id=limit_id, threshold_pct=80, period_key="2026-11"))
        await s.commit()
        count = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(SpendAlert)
                .where(SpendAlert.spend_limit_id == limit_id)
            )
        ).scalar_one()
        assert count == 2


async def test_webhook_delivery_key_is_globally_unique(client) -> None:
    customer_id = await _make_user(client, "bcd-webhook@example.com")
    sm = get_sessionmaker()
    async with sm() as s:
        endpoint = WebhookEndpoint(
            customer_id=customer_id,
            url="https://hooks.example.com/computecoin",
            secret_hash="x" * 64,
            events=["job.completed"],
        )
        s.add(endpoint)
        await s.commit()
        endpoint_id = endpoint.id
        assert endpoint.failure_count == 0
        assert endpoint.active is True

    async with sm() as s:
        s.add(
            WebhookDelivery(
                endpoint_id=endpoint_id,
                event_type="job.completed",
                payload={"job_id": str(uuid.uuid4())},
                delivery_key="job.completed:abc",
            )
        )
        await s.commit()

    async with sm() as s:
        s.add(
            WebhookDelivery(
                endpoint_id=endpoint_id,
                event_type="job.completed",
                payload={},
                delivery_key="job.completed:abc",
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()

    async with sm() as s:
        row = (
            await s.execute(
                sa.select(WebhookDelivery).where(
                    WebhookDelivery.delivery_key == "job.completed:abc"
                )
            )
        ).scalar_one()
        assert row.status == "pending"
        assert row.attempts == 0
        assert row.delivered_at is None


async def test_dispute_evidence_requires_an_existing_dispute(client) -> None:
    """Evidence is anchored to a dispute — a dangling row would be unauditable."""
    sm = get_sessionmaker()
    async with sm() as s:
        await s.execute(sa.text("PRAGMA foreign_keys=ON"))
        s.add(
            DisputeEvidence(
                dispute_id=uuid.uuid4(),
                kind="hash",
                payload={"sha256": "c" * 64},
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_org_invitation_token_hash_unique_and_expiry_is_aware(client) -> None:
    owner_id = await _make_user(client, "bcd-invite@example.com")
    sm = get_sessionmaker()
    async with sm() as s:
        org = Organization(name="Acme", owner_id=owner_id)
        s.add(org)
        await s.flush()
        expires = utcnow() + dt.timedelta(days=7)
        s.add(
            OrganizationInvitation(
                org_id=org.id,
                email="new-hire@example.com",
                role="customer",
                token_hash="t" * 64,
                expires_at=expires,
                invited_by_user_id=owner_id,
            )
        )
        await s.commit()
        org_id = org.id

    async with sm() as s:
        invite = (
            await s.execute(
                sa.select(OrganizationInvitation).where(
                    OrganizationInvitation.org_id == org_id
                )
            )
        ).scalar_one()
        # SQLite drops tzinfo: ensure_aware is how every comparison must be done.
        assert ensure_aware(invite.expires_at) > utcnow()
        assert invite.accepted_at is None

    async with sm() as s:
        org2 = Organization(name="Other", owner_id=owner_id)
        s.add(org2)
        await s.flush()
        s.add(
            OrganizationInvitation(
                org_id=org2.id,
                email="someone@example.com",
                role="customer",
                token_hash="t" * 64,
                expires_at=utcnow() + dt.timedelta(days=1),
                invited_by_user_id=owner_id,
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_sso_connection_and_connect_account_constraints(client) -> None:
    user_id = await _make_user(client, "bcd-sso@example.com")
    provider_id = await _make_provider(user_id)
    sm = get_sessionmaker()
    async with sm() as s:
        org = Organization(name="Enterprise", owner_id=user_id)
        s.add(org)
        await s.flush()
        s.add(
            SsoConnection(
                org_id=org.id,
                provider="oidc",
                issuer="https://idp.example.com",
                client_id="client-123",
                config={"jwks_uri": "https://idp.example.com/jwks"},
            )
        )
        s.add(
            ConnectAccount(provider_id=provider_id, stripe_account_id="acct_test_1")
        )
        await s.commit()

    async with sm() as s:
        account = (
            await s.execute(
                sa.select(ConnectAccount).where(ConnectAccount.provider_id == provider_id)
            )
        ).scalar_one()
        # Payouts stay gated until Stripe says the account is ready.
        assert account.payouts_enabled is False
        assert account.status == "pending"

    # One Connect account per provider: payouts must never fan out.
    async with sm() as s:
        s.add(ConnectAccount(provider_id=provider_id, stripe_account_id="acct_test_2"))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_dataset_and_checkpoint_sizes_are_big_integers(client) -> None:
    customer_id = await _make_user(client, "bcd-dataset@example.com")
    big = 4 * 1024 * 1024 * 1024  # 4 GiB — overflows a 32-bit Integer column
    sm = get_sessionmaker()
    async with sm() as s:
        s.add(
            Dataset(
                customer_id=customer_id,
                object_key="datasets/abc",
                filename="train.jsonl",
                size_bytes=big,
                sha256="d" * 64,
                content_type="application/jsonl",
            )
        )
        job = Job(customer_id=customer_id, workload_type="training", model="echo-sim-v1")
        s.add(job)
        await s.flush()
        s.add(
            TrainingCheckpoint(
                job_id=job.id,
                step=100,
                object_key="ckpt/100",
                sha256="e" * 64,
                size_bytes=big,
                metrics={"loss": 0.42},
            )
        )
        await s.commit()
        job_id = job.id

    async with sm() as s:
        ds = (
            await s.execute(sa.select(Dataset).where(Dataset.customer_id == customer_id))
        ).scalar_one()
        assert ds.size_bytes == big
        # One checkpoint per (job, step) — a replayed emit must not duplicate.
        s.add(
            TrainingCheckpoint(
                job_id=job_id, step=100, object_key="ckpt/100b", sha256="f" * 64
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_chain_reconciliation_run_defaults_to_not_ok(db_engine) -> None:
    """A run is only 'ok' once it finished clean — never optimistically."""
    sm = get_sessionmaker()
    async with sm() as s:
        run = ChainReconciliationRun(chain_id=31337)
        s.add(run)
        await s.commit()
        run_id = run.id

    async with sm() as s:
        loaded = await s.get(ChainReconciliationRun, run_id)
        assert loaded is not None
        assert loaded.ok is False
        assert loaded.finished_at is None
        assert loaded.escrows_checked == 0
        assert loaded.drift_count == 0
        assert ensure_aware(loaded.started_at) <= utcnow()


def test_phase_bcd_settings_exist_with_safe_defaults() -> None:
    assert settings.CHALLENGE_SAMPLE_RATE == pytest.approx(0.02)
    assert settings.REDUNDANCY_SAMPLE_RATE == pytest.approx(0.02)
    assert settings.VERIFICATION_HOLD_PAYOUT is False
    assert settings.REPUTATION_DECAY_DAYS == 90
    assert settings.REPUTATION_MIN_SCORE == pytest.approx(0.0)
    assert settings.SLASH_MAX_FRACTION == pytest.approx(1.0)
    assert settings.WEBHOOK_TIMEOUT_SECONDS == pytest.approx(5.0)
    assert settings.WEBHOOK_MAX_ATTEMPTS == 5
    # SSRF default must be closed.
    assert settings.WEBHOOK_ALLOW_PRIVATE_IPS is False
    assert settings.spend_alert_thresholds == [50, 80, 100]
    assert settings.RATE_LIMIT_REQUIRE_REDIS is False
    assert settings.WS_JOB_PUSH_ENABLED is True

    assert settings.BATCH_MAX_ITEMS == 1000
    assert settings.batch_discount_pct == decimal.Decimal("10")
    assert settings.DATASET_MAX_BYTES >= 1024**3
    assert settings.SSO_ENABLED is False
    assert settings.STRIPE_CONNECT_ENABLED is False
    assert settings.STRIPE_CONNECT_CLIENT_ID == ""

    assert settings.CHAIN_RPC_URL == ""
    assert settings.CHAIN_RECONCILE_ENABLED is False
    assert settings.CHAIN_RECONCILE_INTERVAL_SECONDS == 900


def test_boot_guard_still_rejects_dev_secrets_in_prod() -> None:
    """The Phase B/C/D settings must not have broken the non-dev secret guard."""
    from app.core.config import Settings

    with pytest.raises(ValueError) as exc:
        Settings(
            ENVIRONMENT="production",
            AUTH_SECRET="dev-auth-secret-change-me",
            SESSION_SECRET="dev-session-secret-change-me",
            _env_file=None,
        )
    assert "AUTH_SECRET" in str(exc.value)
    assert "SESSION_SECRET" in str(exc.value)


def test_spend_alert_threshold_parser_rejects_garbage() -> None:
    from app.core.config import Settings

    s = Settings(SPEND_ALERT_THRESHOLDS=" 100, 50,abc,, 50 ,-5,0", _env_file=None)
    assert s.spend_alert_thresholds == [50, 100]
    assert Settings(BATCH_DISCOUNT_PCT="oops", _env_file=None).batch_discount_pct == (
        decimal.Decimal("0")
    )
    assert Settings(BATCH_DISCOUNT_PCT="250", _env_file=None).batch_discount_pct == (
        decimal.Decimal("100")
    )
