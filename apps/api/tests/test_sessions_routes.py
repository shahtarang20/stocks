"""U10 session ROUTES and service against the database (written, NOT run).

Kits are inserted directly (no signing) like test_unit_quotes_settlement. The relay is the default in-process one.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.kits import Kit
from app.models.sessions import SESSION_PENDING, SESSION_STOPPED, STOP_UNCLAIMED, STOP_USER, ComputeSession
from app.services import sessions as svc
from tests.helpers import auth_headers, register_verify_login

pytestmark = pytest.mark.asyncio

PUBKEY = "ab" * 32
IMAGE = "registry.example/shell@sha256:" + "b" * 64


async def _kit(slug="sess-kit", version="1.0.0", manifest=None, status="published") -> str:
    manifest = dict(manifest) if manifest is not None else {"session": {"mode": "terminal"}}
    if "session" in manifest:
        # The router only starts mode "session" Kits as sessions (kit_mode_not_session otherwise).
        manifest.setdefault("mode", "session")
    async with get_sessionmaker()() as db:
        if (await db.execute(select(Kit).where(Kit.slug == slug, Kit.version == version))).scalar_one_or_none() is None:
            db.add(Kit(slug=slug, version=version, name=slug, category="dev", image_digest=IMAGE,
                       manifest=manifest,
                       manifest_sha256="0" * 64, signature="0" * 128, signing_key_id="test", status=status))
            await db.commit()
    return f"{slug}@{version}"


def _body(kit_ref, **over):
    b = {"kit_ref": kit_ref, "max_price_per_gpu_hour": "0.10", "max_duration_seconds": 3600,
         "idle_timeout_seconds": 900, "client_pubkey": PUBKEY}
    b.update(over)
    return b


async def _login(client, email):
    return auth_headers((await register_verify_login(client, email))["access_token"])


async def test_create_get_list_and_ownership(client):
    h = await _login(client, "sess-a@example.com")
    ref = await _kit()
    r = await client.post("/v1/sessions", headers=h, json=_body(ref))
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["status"] == SESSION_PENDING and s["kit_ref"] == ref and s["mode"] == "pty" and s["gpu_count"] == 1
    assert float(s["held"]) > 0 and s["captured"] is None
    got = await client.get(f"/v1/sessions/{s['id']}", headers=h)
    assert got.status_code == 200 and got.json()["id"] == s["id"]
    assert [x["id"] for x in (await client.get("/v1/sessions", headers=h)).json()] == [s["id"]]
    other = await _login(client, "sess-a-other@example.com")
    assert (await client.get(f"/v1/sessions/{s['id']}", headers=other)).status_code == 404
    assert (await client.post(f"/v1/sessions/{s['id']}/stop", headers=other)).status_code == 404
    assert (await client.get(f"/v1/sessions/{uuid.uuid4()}", headers=h)).status_code == 404


@pytest.mark.parametrize("tier", ["sealed", "sealed_pro", "confidential"])
async def test_privacy_tier_refused(client, tier):
    h = await _login(client, f"sess-priv-{tier}@example.com")
    r = await client.post("/v1/sessions", headers=h, json=_body(await _kit(), isolation_tier=tier))
    assert r.status_code == 422 and r.json()["detail"] == "sessions_not_available_for_private_tiers"
    async with get_sessionmaker()() as db:
        assert (await db.execute(select(ComputeSession))).first() is None  # no hold, no row


async def test_field_validation_codes(client):
    h = await _login(client, "sess-val@example.com")
    ref = await _kit()
    cases = [
        (_body(ref, client_pubkey="zz"), "client_pubkey_must_be_32_byte_hex"),
        (_body(ref, max_duration_seconds=10), "max_duration_out_of_range"),
        (_body(ref, idle_timeout_seconds=10), "idle_timeout_out_of_range"),
        (_body(ref, gpu_count=9), "gpu_count_out_of_range"),
        (_body(ref, max_price_per_gpu_hour="0"), "max_price_must_be_positive"),
        (_body(ref, isolation_tier="bogus"), "unsupported_isolation_tier"),
        (_body(None, image_digest=IMAGE), "custom_images_not_enabled_for_sessions"),
        (_body(None), "give_exactly_one_of_kit_ref_or_image_digest"),
    ]
    for body, code in cases:
        body = {k: v for k, v in body.items() if v is not None}
        r = await client.post("/v1/sessions", headers=h, json=body)
        assert r.status_code == 422 and r.json()["detail"] == code, (code, r.text)


async def test_unknown_unpublished_and_invalid_session_block_kits(client):
    h = await _login(client, "sess-kit-bad@example.com")
    r = await client.post("/v1/sessions", headers=h, json=_body("nope@1.0.0"))
    assert r.status_code == 404 and r.json()["detail"] == "kit_not_found"
    yanked = await _kit("yanked-kit", status="yanked")
    assert (await client.post("/v1/sessions", headers=h, json=_body(yanked))).status_code == 404
    bad = await _kit("bad-block-kit", manifest={"session": {"mode": "bogus"}})
    r = await client.post("/v1/sessions", headers=h, json=_body(bad))
    assert r.status_code == 422 and r.json()["detail"] == "kit_session_block_invalid"


async def test_kit_without_session_block_is_refused(client):
    """A batch Kit must not be launchable as an interactive shell. NOTE: create_session currently only validates a
    block when one is present, so this expectation may fail until the router or service refuses a missing block."""
    h = await _login(client, "sess-noblock@example.com")
    ref = await _kit("batch-only-kit", manifest={"outputs": []})
    r = await client.post("/v1/sessions", headers=h, json=_body(ref))
    assert r.status_code in (404, 409, 422), r.text


async def test_kit_egress_cannot_be_widened(client):
    h = await _login(client, "sess-egress@example.com")
    ref = await _kit("egress-kit", manifest={"session": {"mode": "terminal"}, "egress": ["example.org:443"]})
    r = await client.post("/v1/sessions", headers=h, json=_body(ref, network_policy={"egress": ["evil.example:443"]}))
    assert r.status_code == 422 and r.json()["detail"] == "egress_not_declared_by_kit"


async def test_connect_token_is_single_use(client):
    h = await _login(client, "sess-token@example.com")
    sid = (await client.post("/v1/sessions", headers=h, json=_body(await _kit()))).json()["id"]
    r = await client.post(f"/v1/sessions/{sid}/connect-token", headers=h)
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert r.json()["expires_in"] == svc.TOKEN_TTL_SECONDS and len(token) >= 32
    async with get_sessionmaker()() as db:
        assert await svc.consume_token(db, uuid.UUID(sid), "browser", "wrong-token") is None
        assert await svc.consume_token(db, uuid.UUID(sid), "host", token) is None  # wrong side
        first = await svc.consume_token(db, uuid.UUID(sid), "browser", token)
        assert first is not None and str(first.id) == sid
    async with get_sessionmaker()() as db:
        assert await svc.consume_token(db, uuid.UUID(sid), "browser", token) is None  # used
    # a fresh token works again (reconnect), and the old one stays dead
    t2 = (await client.post(f"/v1/sessions/{sid}/connect-token", headers=h)).json()["token"]
    assert t2 != token
    async with get_sessionmaker()() as db:
        assert await svc.consume_token(db, uuid.UUID(sid), "browser", token) is None
        assert await svc.consume_token(db, uuid.UUID(sid), "browser", t2) is not None


async def test_expired_token_is_refused(client):
    h = await _login(client, "sess-token-exp@example.com")
    sid = (await client.post("/v1/sessions", headers=h, json=_body(await _kit()))).json()["id"]
    token = (await client.post(f"/v1/sessions/{sid}/connect-token", headers=h)).json()["token"]
    async with get_sessionmaker()() as db:
        row = await db.get(ComputeSession, uuid.UUID(sid))
        row.browser_token_expires_at = utcnow() - dt.timedelta(seconds=5)
        await db.commit()
    async with get_sessionmaker()() as db:
        assert await svc.consume_token(db, uuid.UUID(sid), "browser", token) is None


async def test_stop_settles_releases_and_is_idempotent(client):
    h = await _login(client, "sess-stop@example.com")
    sid = (await client.post("/v1/sessions", headers=h, json=_body(await _kit()))).json()["id"]
    r = await client.post(f"/v1/sessions/{sid}/stop", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == SESSION_STOPPED and r.json()["stop_reason"] == STOP_USER
    assert r.json()["captured"] == "0.00" or float(r.json()["captured"]) == 0.0  # never started: nothing billed
    again = await client.post(f"/v1/sessions/{sid}/stop", headers=h)
    assert again.status_code == 200 and again.json()["status"] == SESSION_STOPPED
    # a stopped session issues no token
    assert (await client.post(f"/v1/sessions/{sid}/connect-token", headers=h)).status_code == 409


async def test_reaper_stops_an_unclaimed_session(client):
    h = await _login(client, "sess-reap@example.com")
    sid = (await client.post("/v1/sessions", headers=h, json=_body(await _kit()))).json()["id"]
    async with get_sessionmaker()() as db:
        assert await svc.reap(db) == 0  # fresh: nothing due
        row = await db.get(ComputeSession, uuid.UUID(sid))
        row.created_at = utcnow() - dt.timedelta(seconds=svc.UNCLAIMED_SECONDS + 60)
        await db.commit()
    async with get_sessionmaker()() as db:
        assert await svc.reap(db) == 1
        await db.commit()
    got = await client.get(f"/v1/sessions/{sid}", headers=h)
    assert got.json()["status"] == SESSION_STOPPED and got.json()["stop_reason"] == STOP_UNCLAIMED
    async with get_sessionmaker()() as db:
        assert await svc.reap(db) == 0  # idempotent


async def test_live_session_cap_per_user(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "SESSIONS_MAX_LIVE_PER_USER", 1, raising=False)
    h = await _login(client, "sess-cap@example.com")
    ref = await _kit()
    assert (await client.post("/v1/sessions", headers=h, json=_body(ref))).status_code == 201
    r = await client.post("/v1/sessions", headers=h, json=_body(ref))
    assert r.status_code == 429 and r.json()["detail"] == "too_many_live_sessions"


async def test_customer_is_locked_before_the_live_session_count(client, monkeypatch):
    """The per-customer lock must be taken BEFORE live sessions are counted. Counting first and locking after
    lets two concurrent creates both read 'under the cap' (the lock is a Postgres advisory lock, so a race cannot
    be provoked on SQLite; the ORDER of the two steps is what is asserted)."""
    from sqlalchemy import event

    from app.db.session import get_engine
    from app.services import ledger

    events: list[str] = []
    real_lock = ledger.lock_customer

    async def spy(db, user_id):
        events.append("lock")
        return await real_lock(db, user_id)

    monkeypatch.setattr(ledger, "lock_customer", spy)

    def before(conn, cursor, statement, *a):
        if "FROM compute_sessions" in statement and "customer_id" in statement and "SELECT compute_sessions.id" in statement:
            events.append("count_live")

    sync_engine = get_engine().sync_engine
    event.listen(sync_engine, "before_cursor_execute", before)
    try:
        h = await _login(client, "sess-lockorder@example.com")
        r = await client.post("/v1/sessions", headers=h, json=_body(await _kit()))
        assert r.status_code == 201, r.text
    finally:
        event.remove(sync_engine, "before_cursor_execute", before)
    assert "count_live" in events and "lock" in events, events
    assert events.index("lock") < events.index("count_live"), events


async def test_host_ended_with_a_malformed_body_is_a_422_not_a_500(client):
    import hashlib
    import hmac

    from tests.test_phase3_marketplace import create_listable_machine

    machine = await create_listable_machine(client, "sess-ended-prov@example.com", hostname="ended")
    h = await _login(client, "sess-ended@example.com")
    sid = (await client.post("/v1/sessions", headers=h, json=_body(await _kit()))).json()["id"]
    async with get_sessionmaker()() as db:
        row = (await db.execute(select(ComputeSession).where(ComputeSession.id == uuid.UUID(sid)))).scalar_one()
        row.machine_id = uuid.UUID(machine["machine_id"])
        await db.commit()

    def signed(raw: bytes) -> dict:
        ts = str(int(utcnow().timestamp()))
        canonical = f"{machine['machine_id']}\n{ts}\n{hashlib.sha256(raw).hexdigest()}"
        sig = hmac.new(machine["secret"].removeprefix("msk_").encode(), canonical.encode(), hashlib.sha256).hexdigest()
        return {"X-ComputeCoin-Machine": machine["machine_id"], "X-ComputeCoin-Timestamp": ts,
                "X-ComputeCoin-Signature": sig, "Content-Type": "application/json"}

    for raw in (b"{not json", b"[1, 2]", b'"x"'):
        r = await client.post(f"/v1/agent/sessions/{sid}/ended", content=raw, headers=signed(raw))
        assert r.status_code == 422 and r.json()["detail"] == "invalid_body", (raw, r.status_code, r.text)
    raw = b'{"failed": true}'
    r = await client.post(f"/v1/agent/sessions/{sid}/ended", content=raw, headers=signed(raw))
    assert r.status_code == 200, r.text
