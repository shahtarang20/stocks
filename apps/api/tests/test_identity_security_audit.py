"""Regression tests for the identity / privacy bug audit (API security slice).

Each test names the defect it pins. They all fail on the pre-fix code.
"""

from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import select

from app.core import security as security_module
from app.core.machine_auth import build_canonical, compute_signature, verify_signature
from app.core.security import constant_time_compare
from app.db.session import get_sessionmaker
from app.main import app
from app.models.enums import Role, UserStatus
from app.models.user import User
from app.services import storage as storage_module
from app.services.sso import _is_blocked_ip, extract_email, SsoError
from tests.helpers import auth_headers, grant_role, register_verify_login
from tests.test_c2_orgs_sso import (  # noqa: F401  (sso_env is a fixture)
    _accept,
    _complete_callback,
    _create_org,
    _invite,
    _login,
    _setup_connection,
    sso_env,
)
from tests.test_provider_phase2 import (
    _hb_body,
    become_provider,
    enroll_machine,
    heartbeat_headers,
    mint_token,
)


# ---------------------------------------------------------------------------
# compare_digest on attacker-chosen non-ASCII str raised TypeError -> HTTP 500
# ---------------------------------------------------------------------------
def test_machine_signature_check_refuses_non_ascii_instead_of_raising():
    canonical = build_canonical("m", "1", b"{}")
    assert verify_signature("key", canonical, "é" * 64) is False
    assert verify_signature("key", canonical, compute_signature("key", canonical)) is True


def test_constant_time_compare_handles_non_ascii():
    assert constant_time_compare("é", "é") is True
    assert constant_time_compare("é", "a") is False


def test_storage_grant_with_non_ascii_signature_is_rejected_not_raised():
    raw = "upload:11111111-1111-1111-1111-111111111111:99999999999:éé"
    token = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    assert storage_module.verify_upload_grant(token) is None


@pytest.mark.asyncio
async def test_download_route_with_non_ascii_grant_signature_is_401(client):
    raw = "download:11111111-1111-1111-1111-111111111111:99999999999:éé"
    token = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    resp = await client.get(f"/v1/artifacts/download/{token}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_heartbeat_with_non_ascii_signature_header_is_401(client):
    access = await become_provider(client, "audit-prov@example.com")
    raw = (await mint_token(client, access))["enrollment_token"]
    out = await enroll_machine(client, raw, hostname="audit-gpu")
    headers, body = heartbeat_headers(out["machine_id"], out["machine_secret"], _hb_body("online"))
    wire = [(k.encode(), v.encode()) for k, v in headers.items() if k != "X-ComputeCoin-Signature"]
    wire.append((b"X-ComputeCoin-Signature", b"\xe9" * 64))
    resp = await client.post("/v1/agents/heartbeat", content=body, headers=wire)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_cookie_csrf_header_with_non_ascii_is_403_not_500(client):
    tokens = await register_verify_login(client, "audit-csrf@example.com")
    resp = await client.post(
        "/projects",
        json={"name": "p"},
        cookies={"access_token": tokens["access_token"], "csrf_token": tokens["csrf_token"]},
        headers=[(b"x-csrf-token", b"\xe9\xe9\xe9")],
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_artifact_upload_rejects_overlong_kind(client):
    token = storage_module.make_upload_grant(str(uuid.uuid4()), storage_module.grant_expiry())
    resp = await client.put(
        f"/v1/artifacts/upload/{token}",
        content=b"{}",
        headers={"Content-Type": "application/json", "X-Artifact-Kind": "k" * 33},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "invalid_artifact_kind"


# ---------------------------------------------------------------------------
# SSO
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ip",
    [
        "::ffff:169.254.169.254",  # IPv4-mapped metadata service
        "::ffff:127.0.0.1",
        "100.100.100.200",  # carrier-grade NAT space (Alibaba metadata)
        "64:ff9b::a9fe:a9fe",  # NAT64-embedded metadata address
        "2002:a9fe:a9fe::1",  # 6to4-embedded metadata address
        "10.1.2.3",
    ],
)
def test_sso_ssrf_guard_blocks_embedded_and_shared_space_addresses(ip):
    assert _is_blocked_ip(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_sso_ssrf_guard_allows_public_addresses(ip):
    assert _is_blocked_ip(ip) is False


@pytest.mark.parametrize("flag", ["false", "False", "0"])
def test_sso_string_false_email_verified_is_refused(flag):
    with pytest.raises(SsoError) as exc:
        extract_email({"email": "a@example.com", "email_verified": flag})
    assert exc.value.detail == "sso_email_not_verified"
    assert extract_email({"email": "A@Example.com", "email_verified": "true"}) == "a@example.com"


@pytest.mark.asyncio
async def test_sso_login_is_refused_for_a_suspended_member(client, sso_env):  # noqa: F811
    owner = await _login(client, "audit-sso-owner@example.com")
    org_id = await _create_org(client, owner, name="Audit Org")
    conn_id = await _setup_connection(client, owner, org_id)
    member = await _login(client, "audit-sso-member@example.com")
    raw = (await _invite(client, owner, org_id, "audit-sso-member@example.com"))["token"]
    assert (await _accept(client, member, raw)).status_code == 200

    async with get_sessionmaker()() as s:
        user = (await s.execute(select(User).where(User.email == "audit-sso-member@example.com"))).scalar_one()
        user.status = UserStatus.suspended
        await s.commit()

    sso_env.email = "audit-sso-member@example.com"
    cb = await _complete_callback(client, conn_id, sso_env)
    assert cb.status_code == 403, cb.text
    assert cb.json()["detail"] == "sso_account_not_active"
    assert "access_token" not in cb.text


# ---------------------------------------------------------------------------
# Org invitation e-mail: org name is attacker-chosen text
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invitation_email_escapes_org_name_and_flattens_subject(client, monkeypatch):
    sent: list[dict] = []

    async def fake_send_email(**kwargs):
        sent.append(kwargs)
        return True

    from app.api.routers import orgs as orgs_router

    monkeypatch.setattr(orgs_router, "send_email", fake_send_email)
    owner = await _login(client, "audit-inv-owner@example.com")
    org_id = await _create_org(client, owner, name='<a href="https://evil.test">Click</a>\nBcc: x@y.z')
    await _invite(client, owner, org_id, "audit-inv-target@example.com")

    assert len(sent) == 1
    html_body = sent[0]["html_content"]
    assert '<a href="https://evil.test">' not in html_body
    assert "&lt;a href=" in html_body
    assert "\n" not in sent[0]["subject"] and "\r" not in sent[0]["subject"]


# ---------------------------------------------------------------------------
# Login / registration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_login_for_unknown_email_still_burns_a_password_verification(client, monkeypatch):
    calls: list[str] = []
    real = security_module.verify_password

    def spy(password_hash: str, password: str) -> bool:
        calls.append(password)
        return real(password_hash, password)

    monkeypatch.setattr(security_module, "verify_password", spy)
    resp = await client.post("/auth/login", json={"email": "nobody-here@example.com", "password": "whatever-123"})
    assert resp.status_code == 401
    assert calls == ["whatever-123"], "an unknown account must cost one Argon2 verification (timing oracle)"


@pytest.mark.asyncio
async def test_concurrent_duplicate_registration_is_409_not_500(client, monkeypatch):
    body = {"email": "audit-race@example.com", "password": "Sup3rStrongPw!"}
    assert (await client.post("/auth/register", json=body)).status_code == 201

    from app.api.routers import auth as auth_router

    async def lost_the_race(db, email):  # the pre-check ran before the other request committed
        return None

    monkeypatch.setattr(auth_router, "get_user_by_email", lost_the_race)
    resp = await client.post("/auth/register", json=body)
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_cannot_suspend_a_deleted_account(client):
    admin = await register_verify_login(client, "audit-admin@example.com")
    await grant_role("audit-admin@example.com", Role.administrator)
    await register_verify_login(client, "audit-victim@example.com")
    async with get_sessionmaker()() as s:
        user = (await s.execute(select(User).where(User.email == "audit-victim@example.com"))).scalar_one()
        user.status = UserStatus.deleted
        await s.commit()
        uid = user.id
    resp = await client.post(f"/v1/admin/users/{uid}/suspend", headers=auth_headers(admin["access_token"]))
    assert resp.status_code == 409
    async with get_sessionmaker()() as s:
        user = (await s.execute(select(User).where(User.id == uid))).scalar_one()
        assert user.status == UserStatus.deleted


def test_app_has_the_routes_under_test():
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/auth/login" in paths and "/v1/admin/users/{user_id}/suspend" in paths


# ---------------------------------------------------------------------------
# SSO transport: an unreachable / misbehaving IdP is a clean 4xx/5xx SsoError, not an unhandled exception
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_oidc_transport_failures_become_sso_errors(monkeypatch):
    import httpx as _httpx

    from app.services.sso import HttpxOidcClient

    monkeypatch.setattr("app.services.sso.assert_public_http_url", lambda url: None)

    async def boom(self, url, **kw):
        raise _httpx.ConnectTimeout("idp is down")

    monkeypatch.setattr(_httpx.AsyncClient, "post", boom)
    monkeypatch.setattr(_httpx.AsyncClient, "get", boom)
    client = HttpxOidcClient()
    with pytest.raises(SsoError) as exc:
        await client.exchange_code("https://idp.example.com/token", {"code": "x"})
    assert exc.value.detail == "sso_token_exchange_failed" and exc.value.status_code == 502
    with pytest.raises(SsoError) as exc:
        await client.fetch_jwks("https://idp.example.com/jwks")
    assert exc.value.detail == "sso_jwks_fetch_failed"

    class _Html:
        status_code = 200

        @staticmethod
        def json():
            raise ValueError("not json")

    class _List:
        status_code = 200

        @staticmethod
        def json():
            return ["not", "an", "object"]

    for resp in (_Html, _List):
        async def fake_get(self, url, _r=resp, **kw):
            return _r()

        monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)
        with pytest.raises(SsoError) as exc:
            await client.fetch_jwks("https://idp.example.com/jwks")
        assert exc.value.detail == "sso_jwks_fetch_failed"


# ---------------------------------------------------------------------------
# API keys must not carry operator roles from an administrator owner
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_api_key_of_an_administrator_does_not_grant_admin(client):
    tokens = await register_verify_login(client, "audit-admin-key@example.com")
    await grant_role("audit-admin-key@example.com", Role.administrator)
    await grant_role("audit-admin-key@example.com", Role.security_operator)
    token = tokens["access_token"]
    proj = await client.post("/projects", json={"name": "ci"}, headers=auth_headers(token))
    assert proj.status_code in (200, 201), proj.text
    key = await client.post(f"/projects/{proj.json()['id']}/api-keys", json={"name": "ci"}, headers=auth_headers(token))
    assert key.status_code == 201, key.text
    raw = key.json()["api_key"]

    # The session itself is still an administrator ...
    assert (await client.get("/v1/admin/users", headers=auth_headers(token))).status_code == 200
    # ... the leaked-in-CI key is not.
    me = await client.get("/auth/me", headers=auth_headers(raw))
    assert me.status_code == 200
    assert not {"administrator", "security_operator"} & set(me.json()["roles"])
    assert (await client.get("/v1/admin/users", headers=auth_headers(raw))).status_code == 403


# ---------------------------------------------------------------------------
# Password reset: one successful reset retires every other outstanding reset link
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_password_reset_invalidates_other_outstanding_tokens(client):
    from tests.helpers import register_user

    await register_user(client, "audit-reset@example.com")
    first = (await client.post("/auth/password-reset/request", json={"email": "audit-reset@example.com"})).json()["password_reset_token"]
    second = (await client.post("/auth/password-reset/request", json={"email": "audit-reset@example.com"})).json()["password_reset_token"]
    assert first != second

    ok = await client.post("/auth/password-reset/confirm", json={"token": second, "new_password": "BrandNewPw!2345"})
    assert ok.status_code == 200, ok.text
    stale = await client.post("/auth/password-reset/confirm", json={"token": first, "new_password": "AttackerChosen!99"})
    assert stale.status_code == 400
    login = await client.post("/auth/login", json={"email": "audit-reset@example.com", "password": "BrandNewPw!2345"})
    assert login.status_code == 200


# ---------------------------------------------------------------------------
# Slash replay: an idempotent retry of a slash that used the whole cap returns the same event
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_slash_replay_is_idempotent_even_when_the_first_call_used_the_cap(client, monkeypatch):
    from app.core.config import settings
    from tests.test_b3_reputation import _account_balances, _admin_headers, _credit_provider_earnings, _make_provider

    monkeypatch.setattr(settings, "SLASH_MAX_FRACTION", 0.5)
    await register_verify_login(client, "audit-slash-prov@example.com")
    from tests.helpers import get_user_id

    provider_id, _ = await _make_provider(await get_user_id("audit-slash-prov@example.com"))
    await _credit_provider_earnings(provider_id, "100.00")
    admin = await _admin_headers(client, "audit-slash-admin@example.com")
    payload = {"amount": "50.00", "reason": "proven output forgery", "idempotency_key": "audit-slash-0001"}

    first = await client.post(f"/v1/admin/providers/{provider_id}/slash", headers=admin, json=payload)
    assert first.status_code == 201, first.text
    balances = await _account_balances(provider_id)

    again = await client.post(f"/v1/admin/providers/{provider_id}/slash", headers=admin, json=payload)
    assert again.status_code == 201, again.text  # was 409 slash_cap_exceeded
    assert again.json()["slash_id"] == first.json()["slash_id"]
    assert await _account_balances(provider_id) == balances


# ---------------------------------------------------------------------------
# A host must not be able to plant its own attestation challenge through the heartbeat health block
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_heartbeat_cannot_plant_an_attestation_nonce(client):
    from app.db.base import utcnow
    from app.models.provider import ProviderMachine

    access = await become_provider(client, "audit-nonce@example.com")
    raw = (await mint_token(client, access))["enrollment_token"]
    out = await enroll_machine(client, raw, hostname="audit-nonce-gpu")
    body = _hb_body("online")
    body["health"].update(
        {
            "latest_attestation_nonce": "a" * 48,
            "latest_attestation_nonce_created_at": utcnow().isoformat(),
            "latest_attestation_nonce_job_id": str(uuid.uuid4()),
        }
    )
    headers, raw_body = heartbeat_headers(out["machine_id"], out["machine_secret"], body)
    assert (await client.post("/v1/agents/heartbeat", content=raw_body, headers=headers)).status_code == 200

    async with get_sessionmaker()() as s:
        machine = (await s.execute(select(ProviderMachine).where(ProviderMachine.id == uuid.UUID(out["machine_id"])))).scalar_one()
        assert not any(k.startswith("latest_attestation_nonce") for k in (machine.last_health or {}))
        assert machine.last_health["uptime_s"] == 42
