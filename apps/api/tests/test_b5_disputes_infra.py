"""Phase B5 — dispute evidence, strict rate-limit degradation, WebSocket job push.

Proves:
 1. Opening a dispute auto-attaches an evidence snapshot (input/output/hash) and
    pulls in VerificationResult / RedundancyCheck rows when they exist.
 2. A non-owner gets 404 (never 403) on the dispute and its evidence.
 3. The customer's evidence view redacts provider-private pointers; the admin
    view does not.
 4. Admin adjudication resolves, refunds through the billing choke point (ledger
    stays balanced, cumulative cap enforced) and writes an audit row.
 5. The limiter still 429s, holds across a window edge (sliding window), and
    clamps to a strict limit when Redis is mandatory but absent — without
    failing the API closed.
 6. The agent WebSocket rejects a bad HMAC before accept, accepts a valid one,
    answers pings, and a push reaches only the machine it was addressed to.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.api.routers import disputes as disputes_router
from app.api.routers import ws as ws_router
from app.core import rate_limit as rl
from app.core.config import settings
from app.core.machine_auth import build_canonical
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.main import app
from app.models.enums import Role
from app.models.jobs import Artifact, Job
from app.models.reputation import DisputeEvidence
from app.models.system import AuditLog
from app.models.verification import RedundancyCheck, VerificationResult
from app.services.disputes import canonical_sha256
from tests.helpers import auth_headers, grant_role, register_verify_login
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import (
    _artifact_tmp,  # noqa: F401  (autouse fixture: object store + size cap)
    claim,
    post_result,
    seed_echo_model,
    submit_job,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Wiring shim
# ---------------------------------------------------------------------------
# This agent does not own app/main.py (the Integrator wires it). Attach the new
# routers here if they are not already mounted, so the suite exercises the real
# ASGI app either way. Idempotent: once main.py includes them, this is a no-op.
def _ensure_wired() -> None:
    paths = {getattr(r, "path", None) for r in app.routes}
    if "/v1/admin/disputes/{dispute_id}/adjudicate" not in paths:
        app.include_router(disputes_router.admin_router)
    if "/v1/agents/ws" not in paths:
        app.include_router(ws_router.router)


_ensure_wired()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _customer(client: AsyncClient, email: str) -> str:
    return (await register_verify_login(client, email))["access_token"]


async def _admin(client: AsyncClient, email: str) -> str:
    tokens = await register_verify_login(client, email)
    await grant_role(email, Role.administrator)
    return tokens["access_token"]


async def _completed_job(client: AsyncClient, cust: str, machine: dict) -> str:
    sub = await submit_job(client, cust, estimated_minutes=120)
    assert sub.status_code == 201, sub.text
    job_id = sub.json()["job_id"]
    await claim(client, machine["machine_id"], machine["secret"])
    res = await post_result(
        client, machine["machine_id"], machine["secret"], job_id,
        {"status": "success", "execution_mode": "simulated",
         "output": {"text": "ok"}, "metrics": {"duration_seconds": 3600}},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "completed"
    return job_id


def _evidence_by_kind(rows: list[dict]) -> dict[str, dict]:
    return {r["kind"]: r for r in rows}


# ===========================================================================
# 1. evidence is auto-attached on dispute open
# ===========================================================================
async def test_evidence_auto_attached_on_dispute_open(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-b5a@x.com", hostname="b5a")
    cust = await _customer(client, "c-b5a@x.com")
    job_id = await _completed_job(client, cust, m)

    d = await client.post(
        f"/v1/jobs/{job_id}/dispute", json={"reason": "wrong output"},
        headers=auth_headers(cust),
    )
    assert d.status_code == 201, d.text
    dispute_id = d.json()["dispute_id"]

    ev = await client.get(f"/v1/disputes/{dispute_id}/evidence", headers=auth_headers(cust))
    assert ev.status_code == 200, ev.text
    by_kind = _evidence_by_kind(ev.json()["evidence"])
    assert {"input", "output", "hash"} <= set(by_kind), by_kind.keys()

    # The input digest must be recomputable by the customer from what they sent.
    assert by_kind["input"]["payload"]["input_sha256"] == canonical_sha256({"prompt": "Hello"})
    # Honesty: the simulated run stays stamped simulated in the record.
    assert by_kind["output"]["payload"]["execution_mode"] == "simulated"
    assert by_kind["output"]["payload"]["output_sha256"] is not None
    assert by_kind["hash"]["payload"]["artifact_count"] >= 0

    # Rows really landed in dispute_evidence (not just shaped in the response).
    sm = get_sessionmaker()
    async with sm() as db:
        rows = (
            await db.execute(
                select(DisputeEvidence).where(
                    DisputeEvidence.dispute_id == uuid.UUID(dispute_id)
                )
            )
        ).scalars().all()
        assert len(rows) >= 3


async def test_evidence_pulls_in_verification_and_redundancy_rows(client):
    """A job with verification history must carry it into the dispute record."""
    await seed_echo_model()
    m = await create_listable_machine(client, "p-b5v@x.com", hostname="b5v")
    cust = await _customer(client, "c-b5v@x.com")
    job_id = await _completed_job(client, cust, m)

    sm = get_sessionmaker()
    async with sm() as db:
        db.add(
            VerificationResult(
                job_id=uuid.UUID(job_id), kind="challenge", passed=False,
                expected_hash="a" * 64, actual_hash="b" * 64,
                detail={"mode": "exact"},
            )
        )
        db.add(
            RedundancyCheck(
                primary_job_id=uuid.UUID(job_id), status="mismatched",
                matched=False, detail={"note": "shadow disagreed"},
            )
        )
        await db.commit()

    d = await client.post(
        f"/v1/jobs/{job_id}/dispute", json={"reason": "failed verification"},
        headers=auth_headers(cust),
    )
    assert d.status_code == 201, d.text
    ev = await client.get(
        f"/v1/disputes/{d.json()['dispute_id']}/evidence", headers=auth_headers(cust)
    )
    by_kind = _evidence_by_kind(ev.json()["evidence"])
    assert "challenge" in by_kind
    assert by_kind["challenge"]["payload"]["results"][0]["passed"] is False
    assert "redundancy" in by_kind
    assert by_kind["redundancy"]["payload"]["checks"][0]["status"] == "mismatched"


# ===========================================================================
# 2 + 3. isolation and cross-party redaction
# ===========================================================================
async def test_non_owner_gets_404_on_dispute_and_evidence(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-b5i@x.com", hostname="b5i")
    cust_a = await _customer(client, "c-b5i-a@x.com")
    cust_b = await _customer(client, "c-b5i-b@x.com")
    job_id = await _completed_job(client, cust_a, m)

    d = await client.post(
        f"/v1/jobs/{job_id}/dispute", json={"reason": "mine"}, headers=auth_headers(cust_a)
    )
    dispute_id = d.json()["dispute_id"]

    # 404, never 403 — a 403 would confirm the id exists.
    assert (
        await client.get(f"/v1/disputes/{dispute_id}", headers=auth_headers(cust_b))
    ).status_code == 404
    assert (
        await client.get(f"/v1/disputes/{dispute_id}/evidence", headers=auth_headers(cust_b))
    ).status_code == 404
    # And a plain customer cannot reach the admin evidence view at all.
    assert (
        await client.get(
            f"/v1/admin/disputes/{dispute_id}/evidence", headers=auth_headers(cust_b)
        )
    ).status_code == 403


async def test_customer_evidence_redacts_provider_private_fields(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-b5r@x.com", hostname="b5r")
    cust = await _customer(client, "c-b5r@x.com")
    admin = await _admin(client, "a-b5r@x.com")
    job_id = await _completed_job(client, cust, m)

    # Guarantee at least one artifact so the NESTED redaction is actually
    # exercised (object_key lives inside payload["artifacts"][i]).
    sm = get_sessionmaker()
    async with sm() as db:
        db.add(
            Artifact(
                job_id=uuid.UUID(job_id), kind="output",
                object_key="jobs/secret-bucket-path/out.bin",
                content_type="application/octet-stream",
                size_bytes=12, sha256="c" * 64,
            )
        )
        await db.commit()

    d = await client.post(
        f"/v1/jobs/{job_id}/dispute", json={"reason": "redaction"}, headers=auth_headers(cust)
    )
    dispute_id = d.json()["dispute_id"]

    cust_view = _evidence_by_kind(
        (await client.get(f"/v1/disputes/{dispute_id}/evidence", headers=auth_headers(cust)))
        .json()["evidence"]
    )
    admin_view = _evidence_by_kind(
        (
            await client.get(
                f"/v1/admin/disputes/{dispute_id}/evidence", headers=auth_headers(admin)
            )
        ).json()["evidence"]
    )

    # The machine that ran the job is provider-private.
    assert "machine_id" not in cust_view["output"]["payload"]
    assert admin_view["output"]["payload"]["machine_id"] is not None
    # Artifact object keys are storage-internal pointers, not customer data.
    # They are nested one level down, so this also proves redaction recurses.
    admin_arts = admin_view["hash"]["payload"]["artifacts"]
    cust_arts = cust_view["hash"]["payload"]["artifacts"]
    assert len(admin_arts) == len(cust_arts) >= 1
    assert all("object_key" in a for a in admin_arts)
    assert all("object_key" not in a for a in cust_arts)
    # The integrity hash itself is NOT secret — both sides must be able to check it.
    assert all(a["sha256"] for a in cust_arts)
    assert "secret-bucket-path" not in json.dumps(cust_view)
    # The customer still sees their own input digest.
    assert cust_view["input"]["payload"]["input_sha256"]


# ===========================================================================
# 4. admin adjudication: audited, refund through the billing choke point
# ===========================================================================
async def test_admin_adjudicate_refunds_through_billing_and_audits(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-b5x@x.com", hostname="b5x")
    cust = await _customer(client, "c-b5x@x.com")
    admin = await _admin(client, "a-b5x@x.com")
    job_id = await _completed_job(client, cust, m)

    before = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    d = await client.post(
        f"/v1/jobs/{job_id}/dispute", json={"reason": "bad"}, headers=auth_headers(cust)
    )
    dispute_id = d.json()["dispute_id"]

    res = await client.post(
        f"/v1/admin/disputes/{dispute_id}/adjudicate",
        json={"resolution": "partial refund granted", "refund_amount": "0.50"},
        headers=auth_headers(admin),
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "resolved"
    assert res.json()["refund_amount"] == "0.50"

    # Money moved through the ledger: balance up by exactly 0.50, books balanced.
    after = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    import decimal

    assert decimal.Decimal(after["balance"]) - decimal.Decimal(before["balance"]) == decimal.Decimal("0.50")
    recon = (await client.get("/v1/admin/reconciliation", headers=auth_headers(admin))).json()
    assert recon["balanced"] is True and recon["imbalance"] == "0.00"

    # Audited.
    sm = get_sessionmaker()
    async with sm() as db:
        rows = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action == "admin.dispute.resolve",
                    AuditLog.resource_id == dispute_id,
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].reason == "partial refund granted"

    # Re-adjudicating a resolved dispute is refused (no double refund).
    again = await client.post(
        f"/v1/admin/disputes/{dispute_id}/adjudicate",
        json={"resolution": "again", "refund_amount": "0.50"},
        headers=auth_headers(admin),
    )
    assert again.status_code == 409
    assert again.json()["detail"] == "dispute_not_open"


async def test_adjudicate_refund_beyond_capture_is_refused(client):
    """The cumulative refund cap lives in billing.refund — we must not bypass it."""
    await seed_echo_model()
    m = await create_listable_machine(client, "p-b5c@x.com", hostname="b5c")
    cust = await _customer(client, "c-b5c@x.com")
    admin = await _admin(client, "a-b5c@x.com")
    job_id = await _completed_job(client, cust, m)
    d = await client.post(
        f"/v1/jobs/{job_id}/dispute", json={"reason": "greedy"}, headers=auth_headers(cust)
    )
    dispute_id = d.json()["dispute_id"]

    res = await client.post(
        f"/v1/admin/disputes/{dispute_id}/adjudicate",
        json={"resolution": "refund everything and more", "refund_amount": "9999.00"},
        headers=auth_headers(admin),
    )
    assert res.status_code == 409, res.text
    assert res.json()["detail"] == "invalid_refund_amount"

    # Nothing was resolved and nothing was paid.
    still = (await client.get(f"/v1/disputes/{dispute_id}", headers=auth_headers(cust))).json()
    assert still["status"] == "open"
    assert still["refund_amount"] is None


async def test_non_admin_cannot_adjudicate(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-b5n@x.com", hostname="b5n")
    cust = await _customer(client, "c-b5n@x.com")
    job_id = await _completed_job(client, cust, m)
    d = await client.post(
        f"/v1/jobs/{job_id}/dispute", json={"reason": "x"}, headers=auth_headers(cust)
    )
    res = await client.post(
        f"/v1/admin/disputes/{d.json()['dispute_id']}/adjudicate",
        json={"resolution": "self-serve refund"}, headers=auth_headers(cust),
    )
    assert res.status_code == 403


# ===========================================================================
# 5. rate limiting
# ===========================================================================
async def test_limiter_still_429s_on_login(client):
    """Regression guard: the existing 429 behaviour must survive the rewrite."""
    saw_429 = False
    for _ in range(settings.login_ip_rate_limit + 5):
        resp = await client.post(
            "/auth/login", json={"email": "nobody-b5@x.com", "password": "Wrong123!!"}
        )
        if resp.status_code == 429:
            saw_429 = True
            break
    assert saw_429, "rate limiter never triggered a 429"


async def test_sliding_window_does_not_allow_an_edge_burst():
    """Two full budgets either side of a window boundary must not both pass.

    A fixed-window counter resets at the boundary and would allow 2*limit in a
    span shorter than one window. The sliding window must not.
    """
    limiter = rl.InProcessRateLimiter()
    key = "edge"
    assert limiter.allow(key, 2, 1) is True
    assert limiter.allow(key, 2, 1) is True
    assert limiter.allow(key, 2, 1) is False
    # Just under the window: still blocked (a fixed window would have reset).
    await asyncio.sleep(0.4)
    assert limiter.allow(key, 2, 1) is False
    # Past the window: the oldest hits aged out, so capacity returns.
    await asyncio.sleep(0.75)
    assert limiter.allow(key, 2, 1) is True


async def test_redis_mandatory_without_redis_degrades_strict_not_closed(monkeypatch):
    """Redis required + unreachable => strict low limit, API still serving."""
    limiter = rl.RateLimiter()
    # Pretend the Redis probe already ran and found nothing.
    limiter._redis_checked = True
    limiter._redis = None
    monkeypatch.setattr(settings, "RATE_LIMIT_REQUIRE_REDIS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")

    assert limiter.redis_mandatory() is True
    assert limiter.effective_limit(100) == 10  # ceil(100/10)
    assert limiter.effective_limit(3) == 1     # floored at STRICT_DEGRADE_MIN

    # It degrades, it does not fail closed: the first request still passes.
    assert limiter.allow("deg", 20, 60) is True
    assert limiter.degraded is True
    allowed = 1 + sum(1 for _ in range(10) if limiter.allow("deg", 20, 60))
    assert allowed == 2, f"expected the strict clamp (20 -> 2), got {allowed}"


async def test_no_degradation_when_redis_is_not_mandatory(monkeypatch):
    """Default config (and dev/test) keeps the full in-process limit."""
    limiter = rl.RateLimiter()
    limiter._redis_checked = True
    limiter._redis = None
    monkeypatch.setattr(settings, "RATE_LIMIT_REQUIRE_REDIS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert limiter.redis_mandatory() is False
    assert limiter.effective_limit(20) == 20
    assert sum(1 for _ in range(25) if limiter.allow("nod", 20, 60)) == 20
    assert limiter.degraded is False

    # Even with the flag on, a dev-like environment never degrades.
    monkeypatch.setattr(settings, "RATE_LIMIT_REQUIRE_REDIS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    assert limiter.redis_mandatory() is False
    assert limiter.effective_limit(20) == 20


# ===========================================================================
# 6. WebSocket job push
# ===========================================================================
def ws_credentials(machine_id: str, machine_secret: str, *, skew: int = 0) -> dict:
    """Sign a handshake exactly like the agent: same canonical, empty body."""
    ts = str(int(utcnow().timestamp()) + skew)
    signing_key = machine_secret.removeprefix("msk_")
    canonical = build_canonical(machine_id, ts, b"")
    sig = hmac.new(signing_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {"machine_id": machine_id, "ts": ts, "signature": sig}


class WSSession:
    """Minimal in-loop ASGI WebSocket driver.

    Starlette's TestClient runs the app in a second thread with its own event
    loop, which the test suite's aiosqlite engine cannot cross. Driving the ASGI
    callable directly keeps everything on one loop.
    """

    def __init__(self, path: str, params: dict[str, str] | None = None) -> None:
        qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        self.scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": qs.encode(),
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
        }
        self._inbound: asyncio.Queue = asyncio.Queue()
        self._outbound: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "WSSession":
        self._task = asyncio.create_task(
            app(self.scope, self._inbound.get, self._outbound.put)
        )
        await self._inbound.put({"type": "websocket.connect"})
        return self

    async def __aexit__(self, *exc) -> None:
        await self._inbound.put({"type": "websocket.disconnect", "code": 1000})
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except Exception:
                pass
            except asyncio.CancelledError:  # pragma: no cover
                pass

    async def recv(self, timeout: float = 5.0) -> dict:
        return await asyncio.wait_for(self._outbound.get(), timeout=timeout)

    async def recv_json(self, timeout: float = 5.0) -> dict:
        msg = await self.recv(timeout)
        assert msg["type"] == "websocket.send", msg
        return json.loads(msg["text"])

    async def send_json(self, payload: dict) -> None:
        await self._inbound.put(
            {"type": "websocket.receive", "text": json.dumps(payload)}
        )


async def test_ws_rejects_bad_hmac_before_accept(client, db_engine):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-ws1@x.com", hostname="ws1")
    creds = ws_credentials(m["machine_id"], m["secret"])
    creds["signature"] = "0" * 64  # tampered

    async with WSSession("/v1/agents/ws", creds) as s:
        msg = await s.recv()
        assert msg["type"] == "websocket.close", msg
        assert msg["code"] == 1008


async def test_ws_rejects_unknown_machine_and_stale_timestamp(client, db_engine):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-ws2@x.com", hostname="ws2")

    # Unknown machine id, signed with a real key.
    bogus = ws_credentials(str(uuid.uuid4()), m["secret"])
    async with WSSession("/v1/agents/ws", bogus) as s:
        assert (await s.recv())["type"] == "websocket.close"

    # Valid signature, but far outside the skew window (replay defence).
    stale = ws_credentials(
        m["machine_id"], m["secret"], skew=-(settings.MACHINE_HMAC_SKEW_SECONDS + 60)
    )
    async with WSSession("/v1/agents/ws", stale) as s:
        assert (await s.recv())["type"] == "websocket.close"


async def test_ws_accepts_valid_hmac_and_answers_ping(client, db_engine):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-ws3@x.com", hostname="ws3")
    ws_router.registry.clear()

    async with WSSession("/v1/agents/ws", ws_credentials(m["machine_id"], m["secret"])) as s:
        assert (await s.recv())["type"] == "websocket.accept"
        hello = await s.recv_json()
        assert hello["type"] == "connected"
        assert hello["machine_id"] == m["machine_id"]
        assert ws_router.registry.connection_count(m["machine_id"]) == 1

        await s.send_json({"type": "ping"})
        assert (await s.recv_json())["type"] == "pong"

    # Clean disconnect deregisters.
    await asyncio.sleep(0.05)
    assert ws_router.registry.connection_count(m["machine_id"]) == 0


async def test_ws_push_reaches_only_the_addressed_machine(client, db_engine):
    await seed_echo_model()
    m1 = await create_listable_machine(client, "p-ws4a@x.com", hostname="ws4a")
    m2 = await create_listable_machine(client, "p-ws4b@x.com", hostname="ws4b")
    ws_router.registry.clear()

    async with WSSession("/v1/agents/ws", ws_credentials(m1["machine_id"], m1["secret"])) as s1:
        assert (await s1.recv())["type"] == "websocket.accept"
        await s1.recv_json()  # connected
        async with WSSession(
            "/v1/agents/ws", ws_credentials(m2["machine_id"], m2["secret"])
        ) as s2:
            assert (await s2.recv())["type"] == "websocket.accept"
            await s2.recv_json()  # connected

            job_id = str(uuid.uuid4())
            delivered = await ws_router.push_job_available(m1["machine_id"], job_id=job_id)
            assert delivered == 1

            frame = await s1.recv_json()
            assert frame["type"] == "job_available"
            assert frame["job_id"] == job_id

            # m2 must get nothing — no cross-provider leakage.
            with pytest.raises(asyncio.TimeoutError):
                await s2.recv(timeout=0.3)


async def test_ws_closed_when_feature_flag_is_off(client, db_engine, monkeypatch):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-ws5@x.com", hostname="ws5")
    monkeypatch.setattr(settings, "WS_JOB_PUSH_ENABLED", False)

    async with WSSession("/v1/agents/ws", ws_credentials(m["machine_id"], m["secret"])) as s:
        msg = await s.recv()
        assert msg["type"] == "websocket.close"
        assert msg["code"] == 1008
    # And the push helper is a no-op while disabled.
    assert await ws_router.push_job_available(m["machine_id"], job_id=str(uuid.uuid4())) == 0


async def test_polling_claim_still_works_alongside_ws(client, db_engine):
    """The WS path is an optimisation; HTTP polling stays the source of truth."""
    await seed_echo_model()
    m = await create_listable_machine(client, "p-ws6@x.com", hostname="ws6")
    cust = await _customer(client, "c-ws6@x.com")
    sub = await submit_job(client, cust, estimated_minutes=5)
    assert sub.status_code == 201, sub.text

    async with WSSession("/v1/agents/ws", ws_credentials(m["machine_id"], m["secret"])) as s:
        assert (await s.recv())["type"] == "websocket.accept"
        await s.recv_json()
        claimed = await claim(client, m["machine_id"], m["secret"])
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["job_id"] == sub.json()["job_id"]

    sm = get_sessionmaker()
    async with sm() as db:
        job = await db.get(Job, uuid.UUID(sub.json()["job_id"]))
        assert job is not None
