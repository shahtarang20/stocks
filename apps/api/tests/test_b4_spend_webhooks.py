"""Phase B4 — spend limits, usage analytics and webhooks.

Every test here fails without the B4 services/routers: the caps, the alert
exactly-once guarantee, the owner scoping, the SSRF refusals, the signature and
the retry curve are all asserted against real behaviour, never mocked away. The
only injected seam is outbound HTTP, so the suite runs offline.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.db.session import get_sessionmaker
from app.main import app
from app.models.billing import UsageRecord
from app.models.jobs import Job
from app.models.spend import SpendAlert, SpendLimit
from app.models.webhooks import WebhookDelivery, WebhookEndpoint
from app.services import ledger
from app.services import spend_limits as spend_service
from app.services import webhooks as webhook_service
from tests.helpers import auth_headers, get_user_id, register_verify_login

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Router mounting
# ---------------------------------------------------------------------------
# app/main.py belongs to the Integrator, so these tests mount the B4 routers
# themselves. The include is idempotent: once main.py wires them, this is a
# no-op and the tests keep passing unchanged.
def _ensure_routers_mounted() -> None:
    from app.api.routers import analytics, spend_limits, webhooks

    mounted = {getattr(route, "path", "") for route in app.router.routes}
    for module, probe in (
        (spend_limits, "/spend-limits"),
        (analytics, "/analytics/usage"),
        (webhooks, "/webhooks/endpoints"),
    ):
        if probe not in mounted:
            app.include_router(module.router)


_ensure_routers_mounted()


@pytest_asyncio.fixture
async def session(db_engine):
    """A plain AsyncSession on the test engine, for service-level tests."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        yield db


async def _seed_customer(email: str) -> uuid.UUID:
    """Create a verified user + their credits account (with the signup grant)."""
    sessionmaker = get_sessionmaker()
    from app.core.security import hash_password
    from app.models.user import User

    async with sessionmaker() as db:
        user = User(
            email=email, password_hash=hash_password("Sup3rStrongPw!"), email_verified=True
        )
        db.add(user)
        await db.flush()
        await ledger.get_or_create_customer_account(db, user.id)
        await db.commit()
        return user.id


async def _post_spend(
    db, customer_id: uuid.UUID, amount_minor: int, *, job_id: uuid.UUID | None = None
):
    """Post a realistic ``hold`` (customer_credits -> holding) for a job."""
    customer = await ledger.get_or_create_customer_account(db, customer_id)
    holding = await ledger.get_platform_account(db, "holding")
    reference = str(job_id or uuid.uuid4())
    return await ledger.post_transaction(
        db,
        kind="hold",
        legs=[(customer, "debit", amount_minor), (holding, "credit", amount_minor)],
        idempotency_key=f"hold:{reference}",
        reference_type="job",
        reference_id=reference,
        actor_user_id=customer_id,
    )


# ===========================================================================
# A. SPEND LIMITS
# ===========================================================================
async def test_spend_limit_crud_is_owner_scoped(client):
    owner = await register_verify_login(client, "b4-owner@example.com")
    other = await register_verify_login(client, "b4-other@example.com")

    created = await client.post(
        "/spend-limits",
        json={"scope": "customer", "period": "monthly", "limit_minor": 50000,
              "alert_thresholds": [50, 80]},
        headers=auth_headers(owner["access_token"]),
    )
    assert created.status_code == 201, created.text
    limit_id = created.json()["id"]
    assert created.json()["owner_id"] == str(await get_user_id("b4-owner@example.com"))

    listed = await client.get("/spend-limits", headers=auth_headers(owner["access_token"]))
    assert [row["id"] for row in listed.json()] == [limit_id]

    # The other tenant must get 404 — never 403, which would confirm existence.
    for method, path in (
        ("get", f"/spend-limits/{limit_id}"),
        ("get", f"/spend-limits/{limit_id}/usage"),
        ("get", f"/spend-limits/{limit_id}/alerts"),
    ):
        resp = await getattr(client, method)(path, headers=auth_headers(other["access_token"]))
        assert resp.status_code == 404, (path, resp.text)
    patched = await client.patch(
        f"/spend-limits/{limit_id}",
        json={"limit_minor": 1},
        headers=auth_headers(other["access_token"]),
    )
    assert patched.status_code == 404
    deleted = await client.delete(
        f"/spend-limits/{limit_id}", headers=auth_headers(other["access_token"])
    )
    assert deleted.status_code == 404

    # And the owner's own listing is untouched by the failed attempts.
    still = await client.get(
        f"/spend-limits/{limit_id}", headers=auth_headers(owner["access_token"])
    )
    assert still.status_code == 200 and still.json()["limit_minor"] == 50000


async def test_duplicate_scope_period_is_rejected_and_api_key_scope_refused(client):
    tokens = await register_verify_login(client, "b4-dupe@example.com")
    headers = auth_headers(tokens["access_token"])
    body = {"scope": "customer", "period": "daily", "limit_minor": 1000}

    assert (await client.post("/spend-limits", json=body, headers=headers)).status_code == 201
    clash = await client.post("/spend-limits", json=body, headers=headers)
    assert clash.status_code == 409, clash.text

    # api_key scope is refused outright: nothing attributes a job to an API key,
    # so such a limit could never be enforced.
    refused = await client.post(
        "/spend-limits",
        json={"scope": "api_key", "scope_id": str(uuid.uuid4()), "period": "daily",
              "limit_minor": 1000},
        headers=headers,
    )
    assert refused.status_code == 422, refused.text


async def test_cap_blocks_an_over_cap_spend(session):
    customer_id = await _seed_customer("b4-cap@example.com")
    session.add(
        SpendLimit(
            scope="customer", scope_id=customer_id, owner_id=customer_id,
            period="monthly", limit_minor=10_000, alert_thresholds=[], enforce=True,
        )
    )
    await session.flush()

    # Under the cap: allowed, then spent.
    await spend_service.check_spend_allowed(session, customer_id, None, 6_000)
    await _post_spend(session, customer_id, 6_000)
    assert await spend_service.period_spend_minor(
        session, customer_id=customer_id, period="monthly"
    ) == 6_000

    # 6_000 + 5_000 > 10_000 -> refused.
    with pytest.raises(spend_service.SpendLimitExceeded) as excinfo:
        await spend_service.check_spend_allowed(session, customer_id, None, 5_000)
    assert excinfo.value.spent_minor == 6_000
    assert excinfo.value.limit_minor == 10_000

    # Exactly to the cap is still allowed (the cap is inclusive).
    await spend_service.check_spend_allowed(session, customer_id, None, 4_000)


async def test_released_hold_lowers_period_spend(session):
    """Spend is ledger-derived, so a release must give the budget back.

    A cached counter would not: this is the regression the ledger-derived
    computation exists to prevent.
    """
    customer_id = await _seed_customer("b4-release@example.com")
    job_id = uuid.uuid4()
    await _post_spend(session, customer_id, 9_000, job_id=job_id)
    assert await spend_service.period_spend_minor(
        session, customer_id=customer_id, period="monthly"
    ) == 9_000

    customer = await ledger.get_or_create_customer_account(session, customer_id)
    holding = await ledger.get_platform_account(session, "holding")
    await ledger.post_transaction(
        session,
        kind="release",
        legs=[(holding, "debit", 9_000), (customer, "credit", 9_000)],
        idempotency_key=f"release:{job_id}",
        reference_type="job",
        reference_id=str(job_id),
    )
    assert await spend_service.period_spend_minor(
        session, customer_id=customer_id, period="monthly"
    ) == 0


async def test_project_scoped_cap_only_counts_that_project(session):
    from app.models.org import Project

    customer_id = await _seed_customer("b4-project@example.com")
    project_a = Project(owner_id=customer_id, name="A")
    project_b = Project(owner_id=customer_id, name="B")
    session.add_all([project_a, project_b])
    await session.flush()

    def _job(project_id):
        return Job(
            customer_id=customer_id, project_id=project_id, workload_type="inference",
            model="tinyllama-1.1b-chat-v1", input={}, status="queued",
        )

    job_a, job_b = _job(project_a.id), _job(project_b.id)
    session.add_all([job_a, job_b])
    await session.flush()

    await _post_spend(session, customer_id, 7_000, job_id=job_a.id)
    await _post_spend(session, customer_id, 3_000, job_id=job_b.id)

    assert await spend_service.period_spend_minor(
        session, customer_id=customer_id, period="monthly",
        scope="project", scope_id=project_a.id,
    ) == 7_000

    session.add(
        SpendLimit(
            scope="project", scope_id=project_a.id, owner_id=customer_id,
            period="monthly", limit_minor=7_500, alert_thresholds=[], enforce=True,
        )
    )
    await session.flush()

    # Project B is unaffected by project A's cap.
    await spend_service.check_spend_allowed(session, customer_id, project_b.id, 50_000)
    with pytest.raises(spend_service.SpendLimitExceeded):
        await spend_service.check_spend_allowed(session, customer_id, project_a.id, 1_000)


async def test_concurrent_spends_cannot_exceed_the_cap(db_engine):
    """The TOCTOU test: 8 concurrent submits against a cap that fits 3.

    Without ``spend_guard`` every task reads "spent 0" and all 8 post a hold.
    """
    customer_id = await _seed_customer("b4-race@example.com")
    sessionmaker = get_sessionmaker()
    cap, each = 3_000, 1_000

    async with sessionmaker() as db:
        db.add(
            SpendLimit(
                scope="customer", scope_id=customer_id, owner_id=customer_id,
                period="monthly", limit_minor=cap, alert_thresholds=[], enforce=True,
            )
        )
        await db.commit()

    async def submit(index: int) -> bool:
        async with sessionmaker() as db:
            try:
                # Mirrors the wiring exactly: commit INSIDE the guarded block.
                async with spend_service.spend_guard(
                    db, customer_id, None, each, alerts=False
                ):
                    await _post_spend(db, customer_id, each, job_id=uuid.uuid4())
                    await db.commit()
            except spend_service.SpendLimitExceeded:
                await db.rollback()
                return False
            return True

    results = await asyncio.gather(*(submit(i) for i in range(8)))

    async with sessionmaker() as db:
        spent = await spend_service.period_spend_minor(
            db, customer_id=customer_id, period="monthly"
        )
    assert sum(results) == 3, f"expected 3 winners, got {sum(results)}"
    assert spent == cap, f"ledger spend {spent} exceeded cap {cap}"


async def test_each_alert_threshold_fires_exactly_once(session):
    customer_id = await _seed_customer("b4-alerts@example.com")
    limit = SpendLimit(
        scope="customer", scope_id=customer_id, owner_id=customer_id,
        period="monthly", limit_minor=10_000, alert_thresholds=[50, 80, 100],
        enforce=False,
    )
    session.add(limit)
    await session.flush()

    async def fired() -> list[int]:
        rows = (
            await session.execute(
                select(SpendAlert.threshold_pct)
                .where(SpendAlert.spend_limit_id == limit.id)
                .order_by(SpendAlert.threshold_pct)
            )
        ).scalars().all()
        return list(rows)

    # 50% crossed.
    await _post_spend(session, customer_id, 5_000)
    created = await spend_service.record_spend_alerts(session, customer_id=customer_id)
    assert [a.threshold_pct for a in created] == [50]
    # Re-running without new spend creates nothing (exactly once).
    assert await spend_service.record_spend_alerts(session, customer_id=customer_id) == []
    assert await fired() == [50]

    # 80% crossed: only the NEW threshold fires.
    await _post_spend(session, customer_id, 3_000)
    created = await spend_service.record_spend_alerts(session, customer_id=customer_id)
    assert [a.threshold_pct for a in created] == [80]
    assert await fired() == [50, 80]

    # 100% crossed.
    await _post_spend(session, customer_id, 2_000)
    created = await spend_service.record_spend_alerts(session, customer_id=customer_id)
    assert [a.threshold_pct for a in created] == [100]
    assert await fired() == [50, 80, 100]

    # Ten more passes still produce nothing — the UNIQUE constraint holds.
    for _ in range(10):
        assert await spend_service.record_spend_alerts(session, customer_id=customer_id) == []
    total = (
        await session.execute(
            select(func.count()).select_from(SpendAlert).where(
                SpendAlert.spend_limit_id == limit.id
            )
        )
    ).scalar_one()
    assert total == 3


async def test_alerts_reset_when_the_period_rolls_over(session):
    customer_id = await _seed_customer("b4-rollover@example.com")
    limit = SpendLimit(
        scope="customer", scope_id=customer_id, owner_id=customer_id,
        period="daily", limit_minor=10_000, alert_thresholds=[50], enforce=False,
    )
    session.add(limit)
    await session.flush()
    await _post_spend(session, customer_id, 6_000)

    today = dt.datetime.now(dt.timezone.utc)
    assert len(await spend_service.record_spend_alerts(
        session, customer_id=customer_id, now=today)) == 1
    assert await spend_service.record_spend_alerts(
        session, customer_id=customer_id, now=today) == []

    # A different day is a different period_key -> the alert may fire again.
    # (Spend still counts because the hold was posted "today"; this asserts the
    # alert bucket rolls, which is the point of period_key.)
    assert spend_service.period_key("daily", today) != spend_service.period_key(
        "daily", today + dt.timedelta(days=1)
    )


async def test_usage_endpoint_reports_ledger_spend(client):
    tokens = await register_verify_login(client, "b4-usage@example.com")
    headers = auth_headers(tokens["access_token"])
    created = await client.post(
        "/spend-limits",
        json={"scope": "customer", "period": "monthly", "limit_minor": 10_000},
        headers=headers,
    )
    limit_id = created.json()["id"]
    customer_id = await get_user_id("b4-usage@example.com")

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        await _post_spend(db, customer_id, 2_500)
        await db.commit()

    usage = await client.get(f"/spend-limits/{limit_id}/usage", headers=headers)
    assert usage.status_code == 200, usage.text
    body = usage.json()
    assert body["spent_minor"] == 2_500
    assert body["spent"] == "25.00"          # decimal string, never a float
    assert body["remaining_minor"] == 7_500
    assert body["used_pct"] == 25


# ===========================================================================
# B. USAGE ANALYTICS
# ===========================================================================
async def _seed_usage(customer_id: uuid.UUID, project_id, model: str, actual_minor: int):
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        job = Job(
            customer_id=customer_id, project_id=project_id, workload_type="inference",
            model=model, input={}, status="succeeded",
        )
        db.add(job)
        await db.flush()
        db.add(
            UsageRecord(
                job_id=job.id, customer_id=customer_id, estimated_minutes=1,
                actual_seconds=42, estimated_cost_minor=actual_minor + 100,
                actual_cost_minor=actual_minor,
            )
        )
        await db.commit()
        return job.id


async def test_analytics_is_owner_scoped_and_groups_usage(client):
    owner = await register_verify_login(client, "b4-an-owner@example.com")
    other = await register_verify_login(client, "b4-an-other@example.com")
    owner_headers = auth_headers(owner["access_token"])
    owner_id = await get_user_id("b4-an-owner@example.com")
    other_id = await get_user_id("b4-an-other@example.com")

    project = await client.post("/projects", json={"name": "P1"}, headers=owner_headers)
    project_id = project.json()["id"]
    other_project = await client.post(
        "/projects", json={"name": "P2"}, headers=auth_headers(other["access_token"])
    )

    await _seed_usage(owner_id, uuid.UUID(project_id), "llama-a", 1_000)
    await _seed_usage(owner_id, uuid.UUID(project_id), "llama-b", 2_000)
    await _seed_usage(other_id, uuid.UUID(other_project.json()["id"]), "llama-a", 9_999)

    total = await client.get("/analytics/usage", headers=owner_headers)
    assert total.status_code == 200, total.text
    summary = total.json()["summary"]
    # The other tenant's 99.99 is absent.
    assert summary["job_count"] == 2
    assert summary["actual_cost_minor"] == 3_000
    assert summary["actual_cost"] == "30.00"

    by_model = await client.get("/analytics/usage?group_by=model", headers=owner_headers)
    buckets = {b["key"]: b["actual_cost"] for b in by_model.json()["buckets"]}
    assert buckets == {"llama-a": "10.00", "llama-b": "20.00"}

    by_project = await client.get("/analytics/usage?group_by=project", headers=owner_headers)
    assert by_project.json()["buckets"][0]["label"] == "P1"

    # Another tenant's project id -> 404, not 403 and not their data.
    leak = await client.get(
        f"/analytics/usage?project_id={other_project.json()['id']}", headers=owner_headers
    )
    assert leak.status_code == 404, leak.text


async def test_analytics_date_range_filter_and_csv_export(client):
    tokens = await register_verify_login(client, "b4-csv@example.com")
    headers = auth_headers(tokens["access_token"])
    customer_id = await get_user_id("b4-csv@example.com")
    await _seed_usage(customer_id, None, "llama-a", 1_500)

    now = dt.datetime.now(dt.timezone.utc)
    future = (now + dt.timedelta(days=1)).isoformat()
    past = (now - dt.timedelta(days=1)).isoformat()
    further = (now + dt.timedelta(days=2)).isoformat()

    inside = await client.get(
        "/analytics/usage", params={"start": past, "end": future}, headers=headers
    )
    assert inside.status_code == 200, inside.text
    assert inside.json()["summary"]["job_count"] == 1

    outside = await client.get(
        "/analytics/usage", params={"start": future, "end": further}, headers=headers
    )
    assert outside.json()["summary"]["job_count"] == 0

    bad_range = await client.get(
        "/analytics/usage", params={"start": future, "end": past}, headers=headers
    )
    assert bad_range.status_code == 422

    csv_resp = await client.get("/analytics/usage.csv?group_by=model", headers=headers)
    assert csv_resp.status_code == 200, csv_resp.text
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_resp.headers["content-disposition"]
    lines = csv_resp.text.strip().splitlines()
    assert lines[0] == "key,label,job_count,actual_seconds,estimated_cost,actual_cost,currency"
    assert lines[1].startswith("llama-a,,1,42,16.00,15.00,")


# ===========================================================================
# C. WEBHOOKS
# ===========================================================================
async def _public_resolver(host: str, port: int) -> list[str]:
    """Pretend every hostname is a public address (tests stay offline)."""
    return ["93.184.216.34"]


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(webhook_service, "_default_resolver", _public_resolver)


@pytest.mark.parametrize(
    "url,code",
    [
        ("http://127.0.0.1/hook", "private_address_blocked"),
        ("http://127.1/hook", "private_address_blocked"),
        ("https://169.254.169.254/latest/meta-data/", "private_address_blocked"),
        ("http://10.0.0.5/hook", "private_address_blocked"),
        ("http://172.16.3.4/hook", "private_address_blocked"),
        ("http://192.168.1.1/hook", "private_address_blocked"),
        ("http://[::1]/hook", "private_address_blocked"),
        ("http://[::ffff:127.0.0.1]/hook", "private_address_blocked"),
        ("http://0.0.0.0/hook", "private_address_blocked"),
        ("file:///etc/passwd", "unsupported_scheme"),
        ("gopher://example.com/x", "unsupported_scheme"),
        ("ftp://example.com/x", "unsupported_scheme"),
        ("https://user:pw@example.com/hook", "url_has_credentials"),
        ("https:///nohost", "invalid_url"),
    ],
)
async def test_ssrf_urls_are_refused(url, code):
    with pytest.raises(webhook_service.WebhookUrlRejected) as excinfo:
        await webhook_service.validate_webhook_url(url)
    assert excinfo.value.code == code


async def test_dns_rebinding_to_metadata_is_refused():
    """A public-looking hostname that RESOLVES to the metadata IP is still blocked."""
    async def evil(host: str, port: int) -> list[str]:
        return ["169.254.169.254"]

    with pytest.raises(webhook_service.WebhookUrlRejected) as excinfo:
        await webhook_service.validate_webhook_url(
            "https://totally-legit.example.com/hook", resolver=evil
        )
    assert excinfo.value.code == "private_address_blocked"


async def test_public_url_is_accepted():
    async def public(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    assert await webhook_service.validate_webhook_url(
        "https://hooks.example.com/x", resolver=public
    ) == ["93.184.216.34"]


async def test_endpoint_create_shows_secret_once_and_is_owner_scoped(client, public_dns):
    owner = await register_verify_login(client, "b4-wh-owner@example.com")
    other = await register_verify_login(client, "b4-wh-other@example.com")
    headers = auth_headers(owner["access_token"])

    created = await client.post(
        "/webhooks/endpoints",
        json={"url": "https://hooks.example.com/a", "events": ["job.completed"]},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    secret = body["secret"]
    assert secret.startswith("whsec_")
    endpoint_id = body["id"]

    # The secret is never retrievable again.
    fetched = await client.get(f"/webhooks/endpoints/{endpoint_id}", headers=headers)
    assert fetched.status_code == 200
    assert "secret" not in fetched.json() and "secret_hash" not in fetched.json()

    # And the raw secret is not what is stored.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        stored = (
            await db.execute(
                select(WebhookEndpoint).where(WebhookEndpoint.id == uuid.UUID(endpoint_id))
            )
        ).scalar_one()
        assert stored.secret_hash != secret
        assert stored.secret_hash == webhook_service.hash_secret(secret)

    # Cross-tenant access is 404.
    for method, path in (
        ("get", f"/webhooks/endpoints/{endpoint_id}"),
        ("get", f"/webhooks/endpoints/{endpoint_id}/deliveries"),
    ):
        resp = await getattr(client, method)(path, headers=auth_headers(other["access_token"]))
        assert resp.status_code == 404


async def test_endpoint_create_refuses_ssrf_url(client):
    tokens = await register_verify_login(client, "b4-wh-ssrf@example.com")
    resp = await client.post(
        "/webhooks/endpoints",
        json={"url": "http://169.254.169.254/latest/", "events": ["job.completed"]},
        headers=auth_headers(tokens["access_token"]),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "webhook_url_rejected:private_address_blocked"

    bad_scheme = await client.post(
        "/webhooks/endpoints",
        json={"url": "file:///etc/passwd", "events": ["job.completed"]},
        headers=auth_headers(tokens["access_token"]),
    )
    assert bad_scheme.status_code == 400
    assert bad_scheme.json()["detail"] == "webhook_url_rejected:unsupported_scheme"


async def test_unknown_event_is_rejected(client, public_dns):
    tokens = await register_verify_login(client, "b4-wh-events@example.com")
    resp = await client.post(
        "/webhooks/endpoints",
        json={"url": "https://hooks.example.com/a", "events": ["job.exploded"]},
        headers=auth_headers(tokens["access_token"]),
    )
    assert resp.status_code == 422, resp.text


async def _make_endpoint(db, customer_id: uuid.UUID, url="https://hooks.example.com/h"):
    secret = webhook_service.generate_secret()
    endpoint = WebhookEndpoint(
        customer_id=customer_id,
        url=url,
        secret_hash=webhook_service.hash_secret(secret),
        events=list(webhook_service.WEBHOOK_EVENTS),
        active=True,
    )
    db.add(endpoint)
    await db.flush()
    return endpoint, secret


class RecordingSender:
    """Injected transport: records every attempt, replays a scripted response."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls: list[tuple[str, dict[str, str], str]] = []

    async def __call__(self, url, headers, body):
        self.calls.append((url, dict(headers), body))
        status = self.statuses[min(len(self.calls) - 1, len(self.statuses) - 1)]
        if isinstance(status, Exception):
            return webhook_service.SendResult(status_code=None, error=str(status))
        return webhook_service.SendResult(status_code=status)


async def test_delivery_signature_verifies_against_the_shown_secret(session, public_dns):
    customer_id = await _seed_customer("b4-sig@example.com")
    endpoint, secret = await _make_endpoint(session, customer_id)
    delivery, created = await webhook_service.enqueue_delivery(
        session, endpoint, event_type="job.completed",
        payload={"job_id": "abc", "status": "succeeded"}, source_id="abc",
    )
    assert created

    sender = RecordingSender([200])
    await webhook_service.attempt_delivery(
        session, delivery, sender=sender, resolver=_public_resolver
    )

    assert delivery.status == "delivered" and delivery.response_status == 200
    _url, headers, body = sender.calls[0]
    timestamp = headers["X-ComputeCoin-Timestamp"]
    signature = headers["X-ComputeCoin-Signature"]
    assert signature.startswith("sha256=")
    # A receiver holding the once-shown secret can verify the raw body.
    assert webhook_service.verify_signature(secret, timestamp, body, signature)
    # Tampering with the body, the timestamp or the secret all fail.
    assert not webhook_service.verify_signature(secret, timestamp, body + "x", signature)
    assert not webhook_service.verify_signature(secret, "0", body, signature)
    assert not webhook_service.verify_signature("whsec_wrong", timestamp, body, signature)


async def test_replayed_delivery_key_does_not_double_deliver(session, public_dns):
    customer_id = await _seed_customer("b4-replay@example.com")
    endpoint, _secret = await _make_endpoint(session, customer_id)
    job_id = "job-42"

    first = await webhook_service.dispatch_event(
        session, customer_id=customer_id, event_type="job.completed",
        payload={"job_id": job_id}, source_id=job_id,
    )
    assert len(first) == 1

    # The producer re-runs (retry, sweeper, at-least-once queue): same source id.
    again = await webhook_service.dispatch_event(
        session, customer_id=customer_id, event_type="job.completed",
        payload={"job_id": job_id}, source_id=job_id,
    )
    assert again == []

    count = (
        await session.execute(
            select(func.count()).select_from(WebhookDelivery).where(
                WebhookDelivery.endpoint_id == endpoint.id
            )
        )
    ).scalar_one()
    assert count == 1

    sender = RecordingSender([200])
    await webhook_service.deliver_due(session, sender=sender, resolver=_public_resolver)
    assert len(sender.calls) == 1
    # A second dispatcher tick must not re-send an already-delivered delivery.
    await webhook_service.deliver_due(session, sender=sender, resolver=_public_resolver)
    assert len(sender.calls) == 1


async def test_retry_backoff_then_give_up(session, public_dns, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEBHOOK_MAX_ATTEMPTS", 3)
    customer_id = await _seed_customer("b4-retry@example.com")
    endpoint, _secret = await _make_endpoint(session, customer_id)
    delivery, _ = await webhook_service.enqueue_delivery(
        session, endpoint, event_type="job.failed", payload={"job_id": "x"}, source_id="x",
    )

    sender = RecordingSender([500, 500, 500])
    start = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)

    await webhook_service.attempt_delivery(
        session, delivery, sender=sender, resolver=_public_resolver, now=start
    )
    assert delivery.status == "pending" and delivery.attempts == 1
    assert delivery.next_attempt_at == start + dt.timedelta(seconds=2)

    await webhook_service.attempt_delivery(
        session, delivery, sender=sender, resolver=_public_resolver, now=start
    )
    assert delivery.status == "pending" and delivery.attempts == 2
    # Exponential, not linear.
    assert delivery.next_attempt_at == start + dt.timedelta(seconds=4)

    await webhook_service.attempt_delivery(
        session, delivery, sender=sender, resolver=_public_resolver, now=start
    )
    assert delivery.attempts == 3 and delivery.status == "failed"
    assert len(sender.calls) == 3

    # A terminal delivery is never retried again.
    await webhook_service.attempt_delivery(
        session, delivery, sender=sender, resolver=_public_resolver, now=start
    )
    assert len(sender.calls) == 3

    # The bodies were byte-identical across retries, so the signature stays valid.
    assert len({body for _u, _h, body in sender.calls}) == 1


async def test_delivery_recovers_and_resets_failure_count(session, public_dns):
    customer_id = await _seed_customer("b4-recover@example.com")
    endpoint, _secret = await _make_endpoint(session, customer_id)
    delivery, _ = await webhook_service.enqueue_delivery(
        session, endpoint, event_type="balance.low", payload={"balance": "1.00"},
        source_id="bal-1",
    )
    sender = RecordingSender([503, 200])
    now = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)

    await webhook_service.attempt_delivery(
        session, delivery, sender=sender, resolver=_public_resolver, now=now
    )
    assert endpoint.failure_count == 1 and delivery.status == "pending"

    await webhook_service.attempt_delivery(
        session, delivery, sender=sender, resolver=_public_resolver, now=now
    )
    assert delivery.status == "delivered"
    assert endpoint.failure_count == 0 and endpoint.last_error is None


async def test_delivery_to_a_rebound_private_host_is_blocked_and_sink_disabled(session):
    """SSRF is re-checked at SEND time, not only at registration."""
    customer_id = await _seed_customer("b4-rebind@example.com")
    endpoint, _secret = await _make_endpoint(session, customer_id)
    delivery, _ = await webhook_service.enqueue_delivery(
        session, endpoint, event_type="job.completed", payload={}, source_id="j1",
    )

    async def rebound(host: str, port: int) -> list[str]:
        return ["169.254.169.254"]

    sender = RecordingSender([200])
    await webhook_service.attempt_delivery(
        session, delivery, sender=sender, resolver=rebound
    )

    assert sender.calls == [], "no HTTP request may be made to a private address"
    assert delivery.status == "failed"
    assert delivery.last_error == "ssrf_blocked:private_address_blocked"
    assert endpoint.active is False


async def test_dispatch_only_reaches_subscribed_active_endpoints(session, public_dns):
    customer_id = await _seed_customer("b4-fanout@example.com")
    subscribed, _ = await _make_endpoint(session, customer_id, "https://hooks.example.com/1")
    subscribed.events = ["job.completed"]
    unsubscribed, _ = await _make_endpoint(session, customer_id, "https://hooks.example.com/2")
    unsubscribed.events = ["invoice.paid"]
    disabled, _ = await _make_endpoint(session, customer_id, "https://hooks.example.com/3")
    disabled.events = ["job.completed"]
    disabled.active = False
    other_customer = await _seed_customer("b4-fanout-other@example.com")
    foreign, _ = await _make_endpoint(session, other_customer, "https://hooks.example.com/4")
    await session.flush()

    created = await webhook_service.dispatch_event(
        session, customer_id=customer_id, event_type="job.completed",
        payload={"job_id": "j"}, source_id="j",
    )
    assert [d.endpoint_id for d in created] == [subscribed.id]

    with pytest.raises(ValueError):
        await webhook_service.dispatch_event(
            session, customer_id=customer_id, event_type="not.an.event",
            payload={}, source_id="x",
        )
