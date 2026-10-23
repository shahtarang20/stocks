"""Integration layer for Phases B/C/D — the wiring, not the features.

Every feature agent shipped its own suite against a self-mounted router, so
those suites stay green whether or not ``app/main.py`` ever learns about them.
This module tests the glue that only the Integrator owns:

* every Phase B/C/D router is reachable on a FRESH ``create_app()`` (a fixture
  that self-mounts into the module-global ``app`` cannot fake this);
* the spend-limit guard actually gates the job-submit path;
* a finished job really does fold into reputation and fan out as a webhook;
* those observational hooks are FAIL-SAFE — a raising hook must not fail the
  job, undo the settlement, or charge the customer twice;
* the provider-policy gate is really consulted by the scheduler.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.db.session import get_sessionmaker
from app.models.billing import LedgerTransaction
from app.models.jobs import Job
from app.models.provider import ProviderMachine
from app.models.reputation import ProviderReputation
from app.models.spend import SpendLimit
from app.models.webhooks import WebhookDelivery, WebhookEndpoint
from tests.helpers import auth_headers, get_user_id
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import (
    _artifact_tmp,  # noqa: F401  (autouse fixture: temp artifact store)
    _customer,
    claim,
    post_result,
    seed_echo_model,
    submit_job,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _run_job_to_completion(client, machine: dict, token: str, **overrides) -> uuid.UUID:
    """Submit, claim and report success — the full happy path through the hooks."""
    sub = await submit_job(client, token, machine_id=machine["machine_id"], **overrides)
    assert sub.status_code == 201, sub.text
    job_id = uuid.UUID(sub.json()["job_id"])
    assert (await claim(client, machine["machine_id"], machine["secret"])).status_code == 200
    res = await post_result(
        client,
        machine["machine_id"],
        machine["secret"],
        job_id,
        {
            "status": "success",
            "execution_mode": "simulated",
            "output": {"text": "Hello"},
            "metrics": {"duration_seconds": 3600},
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "completed"
    return job_id


async def _seed_endpoint(customer_id: uuid.UUID, events: list[str]) -> uuid.UUID:
    """Register a sink directly.

    The HTTP create path runs a DNS-resolving SSRF pre-flight (covered by the B4
    suite); this module is about the fan-out hook, so it stays offline.
    """
    from app.services import webhooks as webhook_service

    sm = get_sessionmaker()
    async with sm() as s:
        endpoint = WebhookEndpoint(
            customer_id=customer_id,
            url="https://hooks.example.com/computecoin",
            secret_hash=webhook_service.hash_secret("whsec_integration"),
            events=events,
            active=True,
        )
        s.add(endpoint)
        await s.commit()
        return endpoint.id


async def _deliveries(endpoint_id: uuid.UUID) -> list[WebhookDelivery]:
    sm = get_sessionmaker()
    async with sm() as s:
        return list(
            (
                await s.execute(
                    select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint_id)
                )
            ).scalars().all()
        )


async def _txn_count(job_id: uuid.UUID, kind: str) -> int:
    sm = get_sessionmaker()
    async with sm() as s:
        return int(
            (
                await s.execute(
                    select(func.count(LedgerTransaction.id)).where(
                        LedgerTransaction.kind == kind,
                        LedgerTransaction.reference_type == "job",
                        LedgerTransaction.reference_id == str(job_id),
                    )
                )
            ).scalar()
            or 0
        )


# ===========================================================================
# 1. Router wiring
# ===========================================================================
async def test_every_phase_bcd_router_is_wired_into_create_app():
    """A fresh app built from main.py exposes every Phase B/C/D route.

    Built with ``create_app()`` on purpose: the feature suites mount their
    routers onto the module-global ``app`` at import time, so asserting against
    that object would pass even with main.py untouched.
    """
    from app.main import create_app

    fresh = create_app()
    paths = {getattr(route, "path", "") for route in fresh.routes}

    for path in (
        "/v1/admin/verification/challenges",      # B2
        "/v1/jobs/{job_id}/verification-results",  # B2
        "/v1/provider/reputation",                 # B3
        "/v1/admin/providers/{provider_id}/slash",  # B3
        "/spend-limits",                           # B4
        "/analytics/usage",                        # B4
        "/webhooks/endpoints",                     # B4
        "/v1/admin/disputes/{dispute_id}/adjudicate",  # B5
        "/v1/agents/ws",                           # B5
        "/v1/batches",                             # C1
        "/v1/datasets",                            # C1
        "/orgs",                                   # C2
        "/sso/oidc/callback",                      # C2
        "/v1/provider/machines/{machine_id}/pricing",  # C3
        "/v1/provider/connect/onboarding",         # C3
        "/v1/chain/reconciliation/runs",           # D2
    ):
        assert path in paths, f"{path} is not wired into create_app()"

    # And the pre-existing surface still works.
    assert {"/health", "/metrics", "/v1/jobs", "/auth/login"} <= paths


async def test_no_duplicate_route_registrations():
    """Two routers claiming the same (method, path) would silently shadow one."""
    from app.main import create_app

    seen: set[tuple[str, str]] = set()
    clashes: list[tuple[str, str]] = []
    for route in create_app().routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", None) or {"WEBSOCKET"}:
            if (method, path) in seen:
                clashes.append((method, path))
            seen.add((method, path))
    assert clashes == []


# ===========================================================================
# 2. Spend limits gate the submit path (B4 HOOK 1)
# ===========================================================================
async def test_spend_limit_blocks_job_submit_and_holds_no_money(client):
    await seed_echo_model()
    await create_listable_machine(client, "p-int-spend@x.com", hostname="intspend")
    cust = await _customer(client, "c-int-spend@x.com")
    customer_id = await get_user_id("c-int-spend@x.com")

    before = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()

    # A cap of 0.01 cannot accommodate any authorization.
    sm = get_sessionmaker()
    async with sm() as s:
        s.add(
            SpendLimit(
                scope="customer", scope_id=customer_id, owner_id=customer_id,
                period="total", limit_minor=1, enforce=True, active=True,
            )
        )
        await s.commit()

    resp = await submit_job(client, cust, max_cost="5.00")
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "spend_limit_exceeded"

    # The refusal is a policy refusal, not a silent drop: the job is persisted
    # FAILED with the reason, and NOT A CENT was held.
    async with sm() as s:
        job = (
            await s.execute(
                select(Job).where(Job.customer_id == customer_id).order_by(Job.created_at.desc())
            )
        ).scalars().first()
        assert job is not None
        assert job.status == "failed"
        assert job.error_code == "spend_limit_exceeded"
        assert job.machine_id is None
        held = (
            await s.execute(
                select(func.count(LedgerTransaction.id)).where(
                    LedgerTransaction.kind == "hold",
                    LedgerTransaction.reference_id == str(job.id),
                )
            )
        ).scalar()
        assert held == 0

    after = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    assert after["balance"] == before["balance"]
    assert after["reserved"] == before["reserved"]


async def test_submit_succeeds_under_a_generous_spend_limit(client):
    """Negative control: the guard must not block ordinary traffic."""
    await seed_echo_model()
    await create_listable_machine(client, "p-int-ok@x.com", hostname="intok")
    cust = await _customer(client, "c-int-ok@x.com")
    customer_id = await get_user_id("c-int-ok@x.com")

    sm = get_sessionmaker()
    async with sm() as s:
        s.add(
            SpendLimit(
                scope="customer", scope_id=customer_id, owner_id=customer_id,
                period="total", limit_minor=100_000, enforce=True, active=True,
            )
        )
        await s.commit()

    resp = await submit_job(client, cust)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "reserved"


# ===========================================================================
# 3. Terminal-job hooks: reputation + webhooks
# ===========================================================================
async def test_completed_job_folds_into_reputation_and_fans_out_webhooks(client):
    await seed_echo_model()
    machine = await create_listable_machine(client, "p-int-hook@x.com", hostname="inthook")
    cust = await _customer(client, "c-int-hook@x.com")
    customer_id = await get_user_id("c-int-hook@x.com")
    endpoint_id = await _seed_endpoint(customer_id, ["job.completed", "invoice.paid"])

    job_id = await _run_job_to_completion(client, machine, cust)

    sm = get_sessionmaker()
    async with sm() as s:
        # Reputation: the executing provider now has a persisted aggregate.
        m = await s.get(ProviderMachine, uuid.UUID(machine["machine_id"]))
        rep = (
            await s.execute(
                select(ProviderReputation).where(ProviderReputation.provider_id == m.provider_id)
            )
        ).scalars().first()
        assert rep is not None, "job completion did not fold into provider reputation"

    deliveries = await _deliveries(endpoint_id)
    assert sorted(d.event_type for d in deliveries) == ["invoice.paid", "job.completed"]
    assert any(d.payload.get("job_id") == str(job_id) for d in deliveries)


async def test_failed_job_emits_job_failed_and_no_invoice_event(client):
    await seed_echo_model()
    machine = await create_listable_machine(client, "p-int-fail@x.com", hostname="intfail")
    cust = await _customer(client, "c-int-fail@x.com")
    customer_id = await get_user_id("c-int-fail@x.com")
    endpoint_id = await _seed_endpoint(
        customer_id, ["job.completed", "job.failed", "invoice.paid"]
    )

    sub = await submit_job(client, cust, machine_id=machine["machine_id"])
    job_id = uuid.UUID(sub.json()["job_id"])
    assert (await claim(client, machine["machine_id"], machine["secret"])).status_code == 200
    res = await post_result(
        client, machine["machine_id"], machine["secret"], job_id,
        {"status": "failed", "error": {"code": "oom", "message": "out of memory"}},
    )
    assert res.status_code == 200, res.text

    deliveries = await _deliveries(endpoint_id)
    assert sorted(d.event_type for d in deliveries) == ["job.failed"]
    assert deliveries[0].payload["job_id"] == str(job_id)
    # A failed job is never charged.
    assert await _txn_count(job_id, "capture") == 0


async def test_a_raising_hook_never_breaks_the_job_or_double_charges(client, monkeypatch):
    """The fail-safe contract: hooks are reporting, not bookkeeping.

    Without the savepoint isolation in ``app.services.hooks.safe_hook`` the
    poisoned transaction would take the settlement down with it.
    """
    await seed_echo_model()
    machine = await create_listable_machine(client, "p-int-boom@x.com", hostname="intboom")
    cust = await _customer(client, "c-int-boom@x.com")

    import app.api.routers.agents as agents_router

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("sink registry exploded")

    monkeypatch.setattr(agents_router, "dispatch_event", _boom)
    monkeypatch.setattr(agents_router.reputation_svc, "record_job_outcome", _boom)

    before = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    job_id = await _run_job_to_completion(client, machine, cust)

    # Billed exactly once, despite both hooks raising.
    assert await _txn_count(job_id, "hold") == 1
    assert await _txn_count(job_id, "capture") == 1
    after = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    assert after["reserved"] == "0.00"
    assert after["balance"] != before["balance"]

    sm = get_sessionmaker()
    async with sm() as s:
        job = await s.get(Job, job_id)
        assert job.status == "completed"


# ===========================================================================
# 4. Provider policy gate is consulted by the scheduler (C3 hook)
# ===========================================================================
async def test_scheduler_skips_a_machine_outside_its_availability_window(client):
    await seed_echo_model()
    machine = await create_listable_machine(client, "p-int-win@x.com", hostname="intwin")
    cust = await _customer(client, "c-int-win@x.com")

    sm = get_sessionmaker()
    async with sm() as s:
        m = await s.get(ProviderMachine, uuid.UUID(machine["machine_id"]))
        # A window that can never contain "now" in any timezone: zero width.
        m.availability_windows = [
            {"days": ["mon"], "start": "03:00", "end": "03:00", "tz": "UTC"}
        ]
        await s.commit()

    resp = await submit_job(client, cust)
    assert resp.status_code == 201, resp.text
    # No eligible provider -> the job waits in QUEUED rather than being placed
    # on a machine its operator has taken off the roster.
    assert resp.json()["status"] == "queued"
    assert resp.json()["machine_id"] is None


async def test_scheduler_skips_a_machine_priced_below_its_own_floor(client):
    await seed_echo_model()
    machine = await create_listable_machine(
        client, "p-int-floor@x.com", hostname="intfloor", price="1.50"
    )
    cust = await _customer(client, "c-int-floor@x.com")

    sm = get_sessionmaker()
    async with sm() as s:
        m = await s.get(ProviderMachine, uuid.UUID(machine["machine_id"]))
        m.floor_price_per_hour = 9  # well above the 1.50 asking price
        await s.commit()

    resp = await submit_job(client, cust)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "queued", "a machine selling below its floor was scheduled"
