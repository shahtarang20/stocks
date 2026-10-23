"""Phase B3 — provider reputation + slashing.

Every assertion here is about REAL behaviour: reputation derived from real job /
verification / dispute history, scheduler ranking driven by the persisted score,
and slashing as a balanced ledger movement that cannot overdraw a provider.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.billing import Dispute
from app.models.enums import Role
from app.models.jobs import Job
from app.models.provider import Provider, ProviderMachine
from app.models.reputation import ReputationEvent, SlashEvent
from app.models.verification import VerificationResult
from app.services import ledger
from app.services import reputation as reputation_svc
from app.services.scheduler import _Candidate, _score_candidates
from tests.helpers import auth_headers, get_user_id, grant_role, register_verify_login

# The reputation router is wired into app/main.py by the integrator; include it
# here if that wiring is not present yet so these tests exercise the real ASGI
# app rather than a stub. Idempotent: never registers the routes twice.
from app.api.routers import reputation as reputation_router  # noqa: E402
from app.main import app  # noqa: E402

if not any(getattr(r, "path", "") == "/v1/provider/reputation" for r in app.routes):
    app.include_router(reputation_router.router)


# ---------------------------------------------------------------------------
# Fixtures / builders (real rows, no mocks)
# ---------------------------------------------------------------------------
async def _make_provider(user_id: uuid.UUID, hostname: str = "box-1") -> tuple[uuid.UUID, uuid.UUID]:
    """Create a provider + one machine; return (provider_id, machine_id)."""
    sm = get_sessionmaker()
    async with sm() as s:
        provider = Provider(user_id=user_id, display_name=hostname)
        s.add(provider)
        await s.flush()
        machine = ProviderMachine(
            provider_id=provider.id,
            hostname=hostname,
            status="registered",
            availability="online",
            asking_price_per_hour=decimal.Decimal("1.50"),
            max_concurrent_jobs=4,
            signing_key="test-signing-key",
            secret_hash="test-secret-hash",
        )
        s.add(machine)
        await s.commit()
        return provider.id, machine.id


async def _add_jobs(
    customer_id: uuid.UUID,
    machine_id: uuid.UUID,
    *,
    completed: int = 0,
    failed: int = 0,
    age_days: float = 1.0,
) -> list[uuid.UUID]:
    sm = get_sessionmaker()
    finished = utcnow() - dt.timedelta(days=age_days)
    ids: list[uuid.UUID] = []
    async with sm() as s:
        for status_, count in (("completed", completed), ("failed", failed)):
            for _ in range(count):
                job = Job(
                    customer_id=customer_id,
                    workload_type="text-generation",
                    model="tinyllama-1.1b-chat-v1",
                    input={"prompt": "hi"},
                    status=status_,
                    machine_id=machine_id,
                    finished_at=finished,
                )
                s.add(job)
                await s.flush()
                ids.append(job.id)
        await s.commit()
    return ids


async def _credit_provider_earnings(provider_id: uuid.UUID, amount: str) -> None:
    """Put real money in the provider's earnings account via the ledger."""
    sm = get_sessionmaker()
    async with sm() as s:
        external = await ledger.get_platform_account(s, "external")
        provider_acct = await ledger.get_provider_account(s, provider_id)
        await ledger.post_transaction(
            s,
            kind="credit_purchase",
            legs=[
                (external, "debit", ledger.to_minor(decimal.Decimal(amount))),
                (provider_acct, "credit", ledger.to_minor(decimal.Decimal(amount))),
            ],
            idempotency_key=f"test_seed_earnings:{provider_id}:{amount}",
        )
        await s.commit()


async def _account_balances(provider_id: uuid.UUID) -> tuple[int, int]:
    sm = get_sessionmaker()
    async with sm() as s:
        provider_acct = await ledger.get_provider_account(s, provider_id)
        platform_rev = await ledger.get_platform_account(s, "platform_revenue")
        await s.commit()
        return int(provider_acct.balance_minor), int(platform_rev.balance_minor)


# ---------------------------------------------------------------------------
# 1. Reputation reflects REAL history
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reputation_derives_from_real_job_history(client: AsyncClient, db_engine):
    auth = await register_verify_login(client, "b3-hist@example.com")
    user_id = await get_user_id("b3-hist@example.com")
    provider_id, machine_id = await _make_provider(user_id)

    sm = get_sessionmaker()
    async with sm() as s:
        # No history at all -> neutral prior (0.5), not a fabricated number.
        empty = await reputation_svc.compute_signals(s, provider_id)
        assert empty.score == decimal.Decimal("0.5000")

    await _add_jobs(user_id, machine_id, completed=9, failed=0)
    async with sm() as s:
        good = await reputation_svc.compute_signals(s, provider_id)
    assert good.completed == 9
    assert good.score > decimal.Decimal("0.80")

    await _add_jobs(user_id, machine_id, completed=0, failed=9)
    async with sm() as s:
        mixed = await reputation_svc.compute_signals(s, provider_id)
    assert mixed.failed == 9
    # Failures pull the score back toward (and below) neutral.
    assert mixed.score < good.score
    assert mixed.score <= decimal.Decimal("0.55")

    # Persisting appends an auditable event and the API reports the stored score.
    async with sm() as s:
        row = await reputation_svc.recompute_reputation(
            s, provider_id, event_kind="job_completed", reason="test_backfill"
        )
        await s.commit()
        assert row.completed_count == 9 and row.failed_count == 9

    res = await client.get("/v1/provider/reputation", headers=auth_headers(auth["access_token"]))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["persisted"] is True
    assert decimal.Decimal(body["score"]) == mixed.score
    assert body["factors"]["completed"] == 9

    events = await client.get(
        f"/v1/providers/{provider_id}/reputation/events",
        headers=auth_headers(auth["access_token"]),
    )
    assert events.status_code == 200
    assert events.json()[0]["kind"] == "job_completed"


@pytest.mark.asyncio
async def test_old_failures_decay(client: AsyncClient, db_engine):
    """A failure from long ago must weigh less than a fresh one (half-life)."""
    await register_verify_login(client, "b3-decay@example.com")
    user_id = await get_user_id("b3-decay@example.com")
    provider_id, machine_id = await _make_provider(user_id)

    sm = get_sessionmaker()
    await _add_jobs(user_id, machine_id, completed=5, failed=0, age_days=0.1)
    await _add_jobs(user_id, machine_id, completed=0, failed=5, age_days=0.1)
    async with sm() as s:
        fresh = await reputation_svc.compute_signals(s, provider_id)

    # Same history, but the failures are three half-lives old.
    auth2_user = await register_verify_login(client, "b3-decay2@example.com")
    user2_id = await get_user_id("b3-decay2@example.com")
    provider2_id, machine2_id = await _make_provider(user2_id, hostname="box-2")
    await _add_jobs(user2_id, machine2_id, completed=5, failed=0, age_days=0.1)
    await _add_jobs(
        user2_id, machine2_id, completed=0, failed=5,
        age_days=3 * 90,  # REPUTATION_DECAY_DAYS default is 90
    )
    async with sm() as s:
        decayed = await reputation_svc.compute_signals(s, provider2_id)

    assert decayed.failed == 5 and fresh.failed == 5
    assert decayed.score > fresh.score
    assert auth2_user["access_token"]


# ---------------------------------------------------------------------------
# 2. A verification failure lowers reputation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verification_failure_lowers_reputation(client: AsyncClient, db_engine):
    await register_verify_login(client, "b3-verif@example.com")
    user_id = await get_user_id("b3-verif@example.com")
    provider_id, machine_id = await _make_provider(user_id)
    job_ids = await _add_jobs(user_id, machine_id, completed=6)

    sm = get_sessionmaker()
    async with sm() as s:
        before = await reputation_svc.recompute_reputation(
            s, provider_id, event_kind="job_completed"
        )
        before_score = decimal.Decimal(before.score)
        await s.commit()

    async with sm() as s:
        result = VerificationResult(
            job_id=job_ids[0],
            kind="challenge",
            passed=False,
            expected_hash="a" * 64,
            actual_hash="b" * 64,
        )
        s.add(result)
        await s.flush()
        row = await reputation_svc.record_verification_outcome(s, result)
        await s.commit()
        after_score = decimal.Decimal(row.score)
        assert row.verification_failures == 1

    assert after_score < before_score

    async with sm() as s:
        from sqlalchemy import select

        events = (
            await s.execute(
                select(ReputationEvent).where(
                    ReputationEvent.provider_id == provider_id,
                    ReputationEvent.kind == "verification_failed",
                )
            )
        ).scalars().all()
    assert len(events) == 1
    assert decimal.Decimal(events[0].delta) < 0
    assert events[0].job_id == job_ids[0]


@pytest.mark.asyncio
async def test_dispute_lost_lowers_reputation(client: AsyncClient, db_engine):
    await register_verify_login(client, "b3-disp@example.com")
    user_id = await get_user_id("b3-disp@example.com")
    provider_id, machine_id = await _make_provider(user_id)
    job_ids = await _add_jobs(user_id, machine_id, completed=6)

    sm = get_sessionmaker()
    async with sm() as s:
        before = (await reputation_svc.compute_signals(s, provider_id)).score
        s.add(
            Dispute(
                job_id=job_ids[0],
                customer_id=user_id,
                reason="bad output",
                status="resolved",
                refund_amount_minor=500,
                resolved_at=utcnow(),
            )
        )
        await s.commit()
    async with sm() as s:
        after = await reputation_svc.compute_signals(s, provider_id)
    assert after.disputes_lost == 1
    assert after.score < before


# ---------------------------------------------------------------------------
# 3. Scheduler ranking uses reputation
# ---------------------------------------------------------------------------
def _cand(price: str, max_slots: int = 4, free: int = 4) -> _Candidate:
    machine = SimpleNamespace(
        id=uuid.uuid4(),
        max_concurrent_jobs=max_slots,
        created_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
    )
    return _Candidate(
        machine=machine,
        used_slots=max_slots - free,
        free_slots=free,
        price=decimal.Decimal(price),
    )


def test_scheduler_prefers_higher_reputation_at_equal_price():
    a, b = _cand("1.50"), _cand("1.50")
    scored = _score_candidates(
        [a, b],
        reliability={a.machine.id: 0.5, b.machine.id: 0.5},
        reputation={a.machine.id: 0.92, b.machine.id: 0.11},
    )
    assert scored[0][0].machine.id == a.machine.id
    assert scored[0][2]["reliability_score"] == 0.92
    assert scored[0][2]["reliability_source"] == "reputation"


def test_persisted_reputation_overrides_completion_history():
    # A's raw completion rate (0.20) is worse than B's (0.60), but A has a
    # persisted reputation of 0.95 (its "failures" were customer-side), so the
    # persisted score must override the raw rate and A must win.
    a, b = _cand("1.50"), _cand("1.50")
    scored = _score_candidates(
        [a, b],
        reliability={a.machine.id: 0.20, b.machine.id: 0.60},
        reputation={a.machine.id: 0.95},
    )
    assert scored[0][0].machine.id == a.machine.id
    by_id = {c.machine.id: factors for c, _s, factors in scored}
    assert by_id[a.machine.id]["reliability_source"] == "reputation"
    assert by_id[a.machine.id]["reliability_score"] == 0.95
    # No persisted row for B -> transparent fallback, not a silent constant.
    assert by_id[b.machine.id]["reliability_source"] == "completion_history"
    assert by_id[b.machine.id]["reliability_score"] == 0.60


@pytest.mark.asyncio
async def test_reputation_by_machine_maps_provider_score(client: AsyncClient, db_engine):
    await register_verify_login(client, "b3-map@example.com")
    user_id = await get_user_id("b3-map@example.com")
    provider_id, machine_id = await _make_provider(user_id)
    await _add_jobs(user_id, machine_id, completed=8)

    sm = get_sessionmaker()
    async with sm() as s:
        row = await reputation_svc.recompute_reputation(
            s, provider_id, event_kind="job_completed"
        )
        await s.commit()
        expected = float(row.score)
    async with sm() as s:
        mapping = await reputation_svc.reputation_by_machine(s, [machine_id])
        unknown = await reputation_svc.reputation_by_machine(s, [uuid.uuid4()])
    assert mapping == {machine_id: expected}
    assert unknown == {}


# ---------------------------------------------------------------------------
# 4-6. Slashing
# ---------------------------------------------------------------------------
async def _admin_headers(client: AsyncClient, email: str = "b3-admin@example.com") -> dict:
    await register_verify_login(client, email)
    await grant_role(email, Role.administrator)
    # Re-login so the fresh JWT carries the administrator role.
    res = await client.post("/auth/login", json={"email": email, "password": "Sup3rStrongPw!"})
    assert res.status_code == 200, res.text
    return auth_headers(res.json()["access_token"])


@pytest.mark.asyncio
async def test_slash_moves_money_and_keeps_ledger_balanced(client: AsyncClient, db_engine):
    provider_auth = await register_verify_login(client, "b3-slash-prov@example.com")
    prov_user_id = await get_user_id("b3-slash-prov@example.com")
    provider_id, machine_id = await _make_provider(prov_user_id)
    await _credit_provider_earnings(provider_id, "100.00")
    await _add_jobs(prov_user_id, machine_id, completed=5)

    before_provider, before_platform = await _account_balances(provider_id)
    admin = await _admin_headers(client)

    res = await client.post(
        f"/v1/admin/providers/{provider_id}/slash",
        headers=admin,
        json={
            "amount": "25.00",
            "reason": "proven output forgery on challenge job",
            "idempotency_key": "slash-case-0001",
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["amount"] == "25.00"
    assert body["ledger_txn_id"]

    after_provider, after_platform = await _account_balances(provider_id)
    assert after_provider == before_provider - 2500
    assert after_platform == before_platform + 2500

    sm = get_sessionmaker()
    async with sm() as s:
        debits, credits = await ledger.ledger_totals(s)
        assert debits == credits  # INVARIANT 2 still holds

        from sqlalchemy import select

        slashes = (
            await s.execute(select(SlashEvent).where(SlashEvent.provider_id == provider_id))
        ).scalars().all()
        assert len(slashes) == 1
        assert slashes[0].amount_minor == 2500
        assert slashes[0].ledger_txn_id is not None

        rep_events = (
            await s.execute(
                select(ReputationEvent).where(
                    ReputationEvent.provider_id == provider_id,
                    ReputationEvent.kind == "slash",
                )
            )
        ).scalars().all()
        assert len(rep_events) == 1

        from app.models.system import AuditLog

        audits = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "provider.slash", AuditLog.outcome == "success"
                )
            )
        ).scalars().all()
        assert len(audits) == 1
        assert "proven output forgery" in (audits[0].reason or "")

    # Replay with the same idempotency key: no second movement of money.
    again = await client.post(
        f"/v1/admin/providers/{provider_id}/slash",
        headers=admin,
        json={
            "amount": "25.00",
            "reason": "proven output forgery on challenge job",
            "idempotency_key": "slash-case-0001",
        },
    )
    assert again.status_code == 201, again.text
    assert again.json()["slash_id"] == body["slash_id"]
    replay_provider, replay_platform = await _account_balances(provider_id)
    assert (replay_provider, replay_platform) == (after_provider, after_platform)
    assert provider_auth["access_token"]


@pytest.mark.asyncio
async def test_slash_beyond_earnings_is_refused(client: AsyncClient, db_engine):
    await register_verify_login(client, "b3-slash-over@example.com")
    prov_user_id = await get_user_id("b3-slash-over@example.com")
    provider_id, _machine_id = await _make_provider(prov_user_id)
    await _credit_provider_earnings(provider_id, "10.00")
    before = await _account_balances(provider_id)

    admin = await _admin_headers(client, "b3-admin2@example.com")
    res = await client.post(
        f"/v1/admin/providers/{provider_id}/slash",
        headers=admin,
        json={
            "amount": "50.00",
            "reason": "attempted overdraw of provider earnings",
            "idempotency_key": "slash-over-0001",
        },
    )
    assert res.status_code == 409, res.text
    # SLASH_MAX_FRACTION (1.0) of the current balance is the cap, so this is
    # refused before the ledger is even touched.
    assert res.json()["detail"] in ("slash_cap_exceeded", "insufficient_earnings")
    assert await _account_balances(provider_id) == before

    sm = get_sessionmaker()
    async with sm() as s:
        from sqlalchemy import select

        assert (
            await s.execute(select(SlashEvent).where(SlashEvent.provider_id == provider_id))
        ).scalars().first() is None
        debits, credits = await ledger.ledger_totals(s)
        assert debits == credits


@pytest.mark.asyncio
async def test_non_admin_cannot_slash(client: AsyncClient, db_engine):
    victim_auth = await register_verify_login(client, "b3-victim@example.com")
    victim_id = await get_user_id("b3-victim@example.com")
    provider_id, _m = await _make_provider(victim_id)
    await _credit_provider_earnings(provider_id, "20.00")
    before = await _account_balances(provider_id)

    attacker = await register_verify_login(client, "b3-attacker@example.com")
    res = await client.post(
        f"/v1/admin/providers/{provider_id}/slash",
        headers=auth_headers(attacker["access_token"]),
        json={
            "amount": "5.00",
            "reason": "unauthorized slash attempt by a plain user",
            "idempotency_key": "slash-attack-0001",
        },
    )
    assert res.status_code == 403, res.text
    # The provider cannot slash itself either.
    self_res = await client.post(
        f"/v1/admin/providers/{provider_id}/slash",
        headers=auth_headers(victim_auth["access_token"]),
        json={
            "amount": "5.00",
            "reason": "self slash attempt by the provider owner",
            "idempotency_key": "slash-self-0001",
        },
    )
    assert self_res.status_code == 403
    assert await _account_balances(provider_id) == before


@pytest.mark.asyncio
async def test_unknown_provider_slash_is_404(client: AsyncClient, db_engine):
    admin = await _admin_headers(client, "b3-admin3@example.com")
    res = await client.post(
        f"/v1/admin/providers/{uuid.uuid4()}/slash",
        headers=admin,
        json={
            "amount": "5.00",
            "reason": "slash against a provider id that does not exist",
            "idempotency_key": "slash-missing-0001",
        },
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# 7. Reputation read authorization
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_provider_cannot_read_another_providers_reputation(
    client: AsyncClient, db_engine
):
    a_auth = await register_verify_login(client, "b3-rep-a@example.com")
    a_id = await get_user_id("b3-rep-a@example.com")
    provider_a, _ma = await _make_provider(a_id, hostname="a-box")

    b_auth = await register_verify_login(client, "b3-rep-b@example.com")
    b_id = await get_user_id("b3-rep-b@example.com")
    provider_b, _mb = await _make_provider(b_id, hostname="b-box")

    own = await client.get(
        f"/v1/providers/{provider_a}/reputation", headers=auth_headers(a_auth["access_token"])
    )
    assert own.status_code == 200
    assert own.json()["provider_id"] == str(provider_a)
    assert own.json()["persisted"] is False  # honest: nothing stored yet

    other = await client.get(
        f"/v1/providers/{provider_b}/reputation", headers=auth_headers(a_auth["access_token"])
    )
    assert other.status_code == 404  # 404, never 403

    other_events = await client.get(
        f"/v1/providers/{provider_b}/reputation/events",
        headers=auth_headers(a_auth["access_token"]),
    )
    assert other_events.status_code == 404

    # Unauthenticated (no bearer header, no session cookie) -> 401, not a leak.
    client.cookies.clear()
    anon = await client.get(f"/v1/providers/{provider_a}/reputation")
    assert anon.status_code == 401
    assert b_auth["access_token"]


@pytest.mark.asyncio
async def test_admin_may_read_any_provider_reputation(client: AsyncClient, db_engine):
    await register_verify_login(client, "b3-rep-c@example.com")
    c_id = await get_user_id("b3-rep-c@example.com")
    provider_c, _mc = await _make_provider(c_id, hostname="c-box")
    admin = await _admin_headers(client, "b3-admin4@example.com")
    res = await client.get(f"/v1/providers/{provider_c}/reputation", headers=admin)
    assert res.status_code == 200
    assert res.json()["provider_id"] == str(provider_c)


@pytest.mark.asyncio
async def test_non_provider_gets_403_on_self_reputation(client: AsyncClient, db_engine):
    auth = await register_verify_login(client, "b3-noprov@example.com")
    res = await client.get(
        "/v1/provider/reputation", headers=auth_headers(auth["access_token"])
    )
    assert res.status_code == 403
