"""Regression tests for the adversarial findings in the Phase B/C/D code.

Each test below FAILS on the pre-fix code. They are grouped by the defect they
pin down rather than by module, because several findings share one root cause
(the spend cap being enforced somewhere other than where the money moves).
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

import pytest

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.enums import Role
from app.models.jobs import Job
from app.models.marketplace import CURRENCY, Quote
from app.models.provider import Provider, ProviderMachine
from app.models.reputation import SlashEvent
from app.models.spend import SpendLimit
from app.models.verification import RedundancyCheck
from app.services import billing, ledger
from app.services import job_fsm as fsm
from app.services import reputation as reputation_svc
from app.services import spend_limits, verification, webhooks
from tests.helpers import (
    get_user_id,
    grant_role,
    register_verify_login,
    seed_hardware_model,
)

MODEL = "tinyllama-1.1b-chat-v1"


# ---------------------------------------------------------------------------
# Builders (real rows only)
# ---------------------------------------------------------------------------
async def _user(client, email: str) -> uuid.UUID:
    await register_verify_login(client, email)
    return await get_user_id(email)


async def _machine(user_id: uuid.UUID, *, hostname: str = "box", slots: int = 1):
    """A fully eligible machine: online, capable of text-generation, 8 GPUs."""
    from app.models.provider import HardwareInventory, ProviderCapability

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
            asking_price_per_hour=decimal.Decimal("1.00"),
            max_concurrent_jobs=slots,
            signing_key="k",
            secret_hash="h",
            last_heartbeat_at=utcnow(),
        )
        s.add(machine)
        await s.flush()
        s.add(
            ProviderCapability(
                machine_id=machine.id,
                supported_workloads=["text-generation"],
                supported_isolation_tiers=["container"],
            )
        )
        s.add(HardwareInventory(machine_id=machine.id, gpu_count=8, gpu_model="A100"))
        await s.commit()
        return provider.id, machine.id


async def _job(session, customer_id: uuid.UUID, **kwargs) -> Job:
    job = Job(
        customer_id=customer_id,
        workload_type="text-generation",
        model=MODEL,
        input={"prompt": "hi"},
        status=kwargs.pop("status", fsm.QUEUED),
        **kwargs,
    )
    session.add(job)
    await session.flush()
    return job


def _quote(customer_id: uuid.UUID, machine_id: uuid.UUID, total: str) -> Quote:
    amount = decimal.Decimal(total)
    return Quote(
        customer_id=customer_id,
        machine_id=machine_id,
        workload_type="text-generation",
        gpu_count=1,
        estimated_minutes=60,
        price_per_hour_snapshot=amount,
        platform_fee_rate=decimal.Decimal("0"),
        gpu_cost=amount,
        platform_fee=decimal.Decimal("0"),
        total=amount,
        currency=CURRENCY,
        status="active",
        expires_at=utcnow() + dt.timedelta(hours=1),
    )


# ---------------------------------------------------------------------------
# Findings 1 / 5 / 8 — the cap must be enforced where the HOLD is posted
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hold_is_refused_when_it_would_breach_an_enforcing_limit(client, db_engine):
    """A job with NO max_cost must still be checked against the real quote total.

    Pre-fix the only cap check was in POST /v1/jobs, against
    ``to_minor(max_cost) if max_cost is not None else 0`` — so omitting
    ``max_cost`` reduced the predicate to ``spent > limit`` and a 5,000-unit hold
    sailed past a 1,000-unit budget. The check now runs inside
    ``billing.reserve_for_job`` against the amount actually being held.
    """
    customer_id = await _user(client, "fix-cap@example.com")
    _, machine_id = await _machine(customer_id, hostname="cap-box")

    sm = get_sessionmaker()
    async with sm() as s:
        s.add(
            SpendLimit(
                owner_id=customer_id,
                scope="customer",
                scope_id=customer_id,
                period="daily",
                limit_minor=1_000,
                enforce=True,
                active=True,
            )
        )
        job = await _job(s, customer_id, machine_id=machine_id)  # max_cost is None
        quote = _quote(customer_id, machine_id, "50.00")  # 5_000 minor units
        s.add(quote)
        await s.flush()

        with pytest.raises(spend_limits.SpendLimitExceeded):
            await billing.reserve_for_job(s, job, quote)

        # Nothing was held: the refusal happens before any ledger write.
        assert await billing.get_hold_minor(s, job.id) == 0


@pytest.mark.asyncio
async def test_background_rescheduling_cannot_bypass_the_cap(client, db_engine):
    """``run_scheduler`` (the sweeper path) must surface the cap, not fund past it.

    The exploit was: submit jobs while nothing is schedulable (cap check sees
    zero spend, no hold is posted, job is persisted QUEUED), then let the
    recovery sweeper call ``run_scheduler`` on every QUEUED job and fund them all
    with no cap check anywhere on that path.
    """
    from app.services.scheduler import run_scheduler

    customer_id = await _user(client, "fix-sweeper@example.com")
    _, machine_id = await _machine(customer_id, hostname="sweeper-box", slots=8)

    sm = get_sessionmaker()
    async with sm() as s:
        s.add(
            SpendLimit(
                owner_id=customer_id,
                scope="customer",
                scope_id=customer_id,
                period="daily",
                limit_minor=100,
                enforce=True,
                active=True,
            )
        )
        job = await _job(s, customer_id, gpu_count=1, estimated_minutes=600)
        await s.commit()
        job_id = job.id

    async with sm() as s:
        job = await s.get(Job, job_id)
        assert await run_scheduler(s, job) is False
        await s.commit()

    async with sm() as s:
        job = await s.get(Job, job_id)
        assert job.status == fsm.FAILED
        assert job.error_code == "spend_limit_exceeded"
        assert await billing.get_hold_minor(s, job_id) == 0


# ---------------------------------------------------------------------------
# Finding 3 — an unbilled job must never take a ledger hold
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unbilled_jobs_never_take_a_ledger_hold(client, db_engine):
    """A shadow job's customer is the PRIMARY job's customer.

    If a shadow ever reached ``reserve_for_job`` (the sweeper used to push it
    there) it debited a real customer for work they never submitted — and since
    ``job_result`` skips settlement for unbilled jobs, that hold was never
    released.
    """
    customer_id = await _user(client, "fix-unbilled@example.com")
    _, machine_id = await _machine(customer_id, hostname="shadow-box")

    sm = get_sessionmaker()
    async with sm() as s:
        primary = await _job(s, customer_id, status=fsm.COMPLETED, machine_id=machine_id)
        shadow = await _job(
            s, customer_id, status=fsm.RESERVED, shadow_of_job_id=primary.id
        )
        challenge = await _job(s, customer_id, status=fsm.RESERVED, is_challenge=True)
        quote = _quote(customer_id, machine_id, "10.00")
        s.add(quote)
        await s.flush()

        assert await billing.reserve_for_job(s, shadow, quote) is None
        assert await billing.reserve_for_job(s, challenge, quote) is None
        assert await billing.get_hold_minor(s, shadow.id) == 0
        assert await billing.get_hold_minor(s, challenge.id) == 0


# ---------------------------------------------------------------------------
# Finding 7 — a shadow that lost its placement is abandoned, never re-placed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_abandoned_shadow_resolves_inconclusive_instead_of_being_rescheduled(
    client, db_engine
):
    """Re-placing a shadow without the different-provider constraint is worse
    than not running it: the cheating provider gets cleared by its own output."""
    customer_id = await _user(client, "fix-shadow@example.com")
    _, machine_id = await _machine(customer_id, hostname="shadow-box-2")

    sm = get_sessionmaker()
    async with sm() as s:
        primary = await _job(s, customer_id, status=fsm.COMPLETED, machine_id=machine_id)
        shadow = await _job(
            s,
            customer_id,
            status=fsm.RESERVED,
            shadow_of_job_id=primary.id,
            machine_id=machine_id,
        )
        check = RedundancyCheck(
            primary_job_id=primary.id, shadow_job_id=shadow.id, status="pending"
        )
        s.add(check)
        await s.flush()

        await verification.abandon_unbilled_job(s, shadow)

        assert shadow.status == fsm.CANCELLED
        assert shadow.machine_id is None
        await s.refresh(check)
        assert check.status == "inconclusive"
        assert (check.detail or {}).get("abandoned_reason") == "placement_lost"


@pytest.mark.asyncio
async def test_recovery_sweeper_leaves_a_fresh_shadow_alone(client, db_engine):
    """A directly-placed audit job is not a stuck reservation.

    ``recover_stuck_reserved`` used to see RESERVED + reservation_id NULL, requeue
    it and push it through the paid scheduler. It must now leave a fresh one for
    the agent to claim.
    """
    from app.services.leases import recover_stuck_reserved

    customer_id = await _user(client, "fix-sweeper-shadow@example.com")
    _, machine_id = await _machine(customer_id, hostname="shadow-box-3")

    sm = get_sessionmaker()
    async with sm() as s:
        primary = await _job(s, customer_id, status=fsm.COMPLETED, machine_id=machine_id)
        shadow = await _job(
            s,
            customer_id,
            status=fsm.RESERVED,
            shadow_of_job_id=primary.id,
            machine_id=machine_id,
            max_runtime_seconds=600,
        )
        await s.commit()
        shadow_id = shadow.id

    async with sm() as s:
        await recover_stuck_reserved(s)
        await s.commit()

    async with sm() as s:
        shadow = await s.get(Job, shadow_id)
        assert shadow.status == fsm.RESERVED
        assert shadow.machine_id == machine_id
        # Untouched: no requeue, no quote, no reservation, no hold. Pre-fix the
        # sweeper cleared machine_id and drove it through the paid scheduler,
        # which created a ResourceReservation (and, before the billing fix, a
        # ledger hold against the primary job's customer).
        assert shadow.reservation_id is None
        assert await billing.get_hold_minor(s, shadow_id) == 0


# ---------------------------------------------------------------------------
# Finding 17 — unbilled work consumes real capacity
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_challenge_job_occupies_a_machine_slot(client, db_engine):
    """A challenge pinned to a 1-slot machine must make it ineligible.

    ``active_reservation_slots`` only counts ResourceReservation rows and a
    challenge has none, so the box looked free and a paying job was scheduled on
    top of the platform's own audit.
    """
    from app.services.scheduler import _eligible_machines

    customer_id = await _user(client, "fix-slots@example.com")
    _, machine_id = await _machine(customer_id, hostname="slot-box", slots=1)

    sm = get_sessionmaker()
    async with sm() as s:
        paying = await _job(s, customer_id, gpu_count=1)
        before = await _eligible_machines(s, paying, utcnow())
        assert [c.machine.id for c in before] == [machine_id]

        await _job(
            s, customer_id, status=fsm.RESERVED, is_challenge=True, machine_id=machine_id
        )
        after = await _eligible_machines(s, paying, utcnow())
        assert after == [], "the challenge must consume the machine's only slot"


# ---------------------------------------------------------------------------
# Finding 6 — a provider-less job must not double-count its capture
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_refund_cap_is_not_doubled_when_the_job_has_no_provider(client, db_engine):
    """With no resolvable provider the capture lands entirely in platform_revenue.

    Querying "what did the provider get" and "what did the platform get" against
    the SAME account returned the full capture twice, doubling the cumulative
    refund cap — and platform_revenue is deliberately not a no-negative account,
    so the ledger happily minted the difference as customer credit.
    """
    customer_id = await _user(client, "fix-refund@example.com")

    sm = get_sessionmaker()
    async with sm() as s:
        job = await _job(s, customer_id, status=fsm.COMPLETED)  # machine_id is None
        customer_acct = await ledger.get_or_create_customer_account(s, customer_id)
        holding = await ledger.get_platform_account(s, "holding")
        revenue = await ledger.get_platform_account(s, "platform_revenue")
        await ledger.post_transaction(
            s,
            kind="hold",
            legs=[(customer_acct, "debit", 10_000), (holding, "credit", 10_000)],
            idempotency_key=f"hold:{job.id}",
            reference_type="job",
            reference_id=str(job.id),
        )
        await ledger.post_transaction(
            s,
            kind="capture",
            legs=[(holding, "debit", 10_000), (revenue, "credit", 10_000)],
            idempotency_key=f"capture:{job.id}",
            reference_type="job",
            reference_id=str(job.id),
        )

        await billing.refund(
            s,
            job=job,
            amount_minor=10_000,
            actor_user_id=customer_id,
            idempotency_key="refund:first",
        )
        # The full capture is now refunded; a second refund would mint money.
        with pytest.raises(ValueError, match="invalid_refund_amount"):
            await billing.refund(
                s,
                job=job,
                amount_minor=10_000,
                actor_user_id=customer_id,
                idempotency_key="refund:second",
            )


# ---------------------------------------------------------------------------
# Finding 13 — SLASH_MAX_FRACTION is a cap, not a per-call allowance
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_slashes_against_one_job_accumulate_against_the_cap(client, db_engine):
    """A second slash under a fresh idempotency key used to be fully re-capped.

    The basis (the job's capture leg) never shrinks, so repeating the call drained
    the provider far past SLASH_MAX_FRACTION.
    """
    customer_id = await _user(client, "fix-slash-cust@example.com")
    provider_user = await _user(client, "fix-slash-prov@example.com")
    provider_id, machine_id = await _machine(provider_user, hostname="slash-box")

    sm = get_sessionmaker()
    async with sm() as s:
        job = await _job(s, customer_id, status=fsm.COMPLETED, machine_id=machine_id)
        holding = await ledger.get_platform_account(s, "holding")
        customer_acct = await ledger.get_or_create_customer_account(s, customer_id)
        provider_acct = await ledger.get_provider_account(s, provider_id)
        await ledger.post_transaction(
            s,
            kind="hold",
            legs=[(customer_acct, "debit", 10_000), (holding, "credit", 10_000)],
            idempotency_key=f"hold:{job.id}",
            reference_type="job",
            reference_id=str(job.id),
        )
        await ledger.post_transaction(
            s,
            kind="capture",
            legs=[(holding, "debit", 10_000), (provider_acct, "credit", 10_000)],
            idempotency_key=f"capture:{job.id}",
            reference_type="job",
            reference_id=str(job.id),
        )
        await s.commit()
        job_id = job.id

    cap = int(10_000 * float(settings.SLASH_MAX_FRACTION))
    async with sm() as s:
        await reputation_svc.slash_provider(
            s,
            provider_id=provider_id,
            amount_minor=cap,
            actor_user_id=customer_id,
            reason="fraud",
            idempotency_key="slash-a",
            job_id=job_id,
        )
        await s.commit()

    async with sm() as s:
        with pytest.raises(reputation_svc.SlashRefused, match="slash_cap_exceeded"):
            await reputation_svc.slash_provider(
                s,
                provider_id=provider_id,
                amount_minor=cap,
                actor_user_id=customer_id,
                reason="fraud again",
                idempotency_key="slash-b",
                job_id=job_id,
            )

    async with sm() as s:
        total = sum(
            int(e.amount_minor)
            for e in (await s.execute(__import__("sqlalchemy").select(SlashEvent))).scalars()
        )
        assert total == cap


# ---------------------------------------------------------------------------
# Finding 14 — a project budget governs the project, not just its author
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_project_limit_counts_every_members_spend(client, db_engine):
    """Alice's project cap must see Bob's jobs, and must apply to Bob."""
    alice = await _user(client, "fix-proj-alice@example.com")
    bob = await _user(client, "fix-proj-bob@example.com")
    project_id = uuid.uuid4()

    sm = get_sessionmaker()
    async with sm() as s:
        s.add(
            SpendLimit(
                owner_id=alice,
                scope="project",
                scope_id=project_id,
                period="monthly",
                limit_minor=1_000,
                enforce=True,
                active=True,
            )
        )
        # Bob spends the whole project budget.
        bob_job = await _job(s, bob, status=fsm.COMPLETED, project_id=project_id)
        bob_acct = await ledger.get_or_create_customer_account(s, bob)
        holding = await ledger.get_platform_account(s, "holding")
        await ledger.post_transaction(
            s,
            kind="hold",
            legs=[(bob_acct, "debit", 1_000), (holding, "credit", 1_000)],
            idempotency_key=f"hold:{bob_job.id}",
            reference_type="job",
            reference_id=str(bob_job.id),
        )
        await s.commit()

    async with sm() as s:
        # Alice's own view of the project's spend includes Bob's.
        spent = await spend_limits.period_spend_minor(
            s, customer_id=alice, period="monthly", scope="project", scope_id=project_id
        )
        assert spent == 1_000

        # And the limit Alice created now applies to Bob too.
        with pytest.raises(spend_limits.SpendLimitExceeded):
            await spend_limits.check_spend_allowed(s, bob, project_id, 1)


@pytest.mark.asyncio
async def test_job_submit_rejects_a_project_the_caller_cannot_use(client, db_engine):
    """An unvalidated ``project_id`` let anyone attach jobs to another's scope.

    ``create_job`` assigned ``body.project_id`` straight from the request with no
    ownership or membership check, so a project budget could be polluted (or a
    scope dodged) with an arbitrary id.
    """
    from tests.helpers import auth_headers

    await seed_hardware_model()
    owner_tokens = await register_verify_login(client, "fix-proj-owner@example.com")
    outsider_tokens = await register_verify_login(client, "fix-proj-outsider@example.com")

    created = await client.post(
        "/projects",
        json={"name": "Private"},
        headers=auth_headers(owner_tokens["access_token"]),
    )
    assert created.status_code in (200, 201), created.text
    project_id = created.json()["id"]

    body = {
        "workload_type": "text-generation",
        "model": MODEL,
        "input": {"prompt": "hi"},
        "gpu_count": 1,
        "estimated_minutes": 10,
        "project_id": project_id,
    }
    resp = await client.post(
        "/v1/jobs", json=body, headers=auth_headers(outsider_tokens["access_token"])
    )
    assert resp.status_code == 404, resp.text

    # The owner may still use their own project.
    ok = await client.post(
        "/v1/jobs", json=body, headers=auth_headers(owner_tokens["access_token"])
    )
    assert ok.status_code == 201, ok.text


# ---------------------------------------------------------------------------
# Finding 15 — the SSRF guard pins the address it vetted
# ---------------------------------------------------------------------------
def test_pinned_sender_dials_the_vetted_address_not_the_hostname():
    """Validating a hostname and then letting httpx resolve it again is a TOCTOU.

    A DNS-rebinding record (TTL 0, alternating public IP / 169.254.169.254)
    passes ``validate_webhook_url`` and is then re-resolved by the HTTP client
    moments later. Pinning removes the second lookup.
    """
    pinned, host_header = webhooks._pin_url("https://evil.test:8443/hook", "93.184.216.34")
    assert pinned == "https://93.184.216.34:8443/hook"
    assert host_header == "evil.test:8443"

    # With nothing vetted (dev mode) there is nothing to pin and the plain
    # sender is used — the behaviour is explicit, not accidental.
    assert webhooks._pinned_sender([]) is webhooks._httpx_sender
    assert webhooks._pinned_sender(["93.184.216.34"]) is not webhooks._httpx_sender


@pytest.mark.asyncio
async def test_attempt_delivery_pins_the_resolved_address(client, db_engine, monkeypatch):
    """The sender must receive a URL whose authority is the vetted IP."""
    from app.models.webhooks import WebhookDelivery, WebhookEndpoint

    customer_id = await _user(client, "fix-ssrf@example.com")
    dialled: list[str] = []

    async def _resolver(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    sm = get_sessionmaker()
    async with sm() as s:
        endpoint = WebhookEndpoint(
            customer_id=customer_id,
            url="http://sink.example.test/hook",
            secret_hash=webhooks.hash_secret("whsec_x"),
            events=["job.completed"],
            active=True,
        )
        s.add(endpoint)
        await s.flush()
        delivery, _ = await webhooks.enqueue_delivery(
            s,
            endpoint,
            event_type="job.completed",
            payload={"job_id": "1"},
            source_id="1",
        )
        await s.flush()

        # No injected sender: this exercises the REAL default send path, with
        # the HTTP client stubbed out at the httpx boundary so we can see which
        # authority it actually dials.
        import httpx

        class _FakeResponse:
            status_code = 200

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def build_request(self, method, url, **kwargs):
                return {"url": url, **kwargs}

            async def send(self, request):
                dialled.append(str(request["url"]))
                return _FakeResponse()

            async def post(self, url, **kwargs):
                dialled.append(str(url))
                return _FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        await webhooks.attempt_delivery(s, delivery, resolver=_resolver)

        assert delivery.status == "delivered"
        # The vetted IP — NOT the hostname, which httpx would have resolved a
        # second time (the rebinding window).
        assert dialled == ["http://93.184.216.34/hook"]
