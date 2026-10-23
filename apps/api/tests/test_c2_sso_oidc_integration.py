"""SSO against a REAL, OpenID-certified IdP (not a stub).

These are INTEGRATION tests. They need an external runtime: a real
panva/node-oidc-provider v9.13.0 instance, which lives in this repo at
``apps/api/tests/oidc_idp``.

Run them like this::

    # terminal 1 — one-time install, then start the real IdP
    cd apps/api/tests/oidc_idp && npm install && PORT=9443 node server.mjs

    # terminal 2
    cd apps/api && SSO_OIDC_INTEGRATION=1 .venv/bin/pytest -q \
        tests/test_c2_sso_oidc_integration.py

Gating, deliberately asymmetric so a fake green is impossible:

* ``SSO_OIDC_INTEGRATION`` unset  -> SKIP, with the command to run above.
* ``SSO_OIDC_INTEGRATION`` set but the IdP unreachable -> FAIL, loudly.
  An opted-in run that silently skips is exactly how a fake green happens.

What is real here: discovery, ``/authorize``, the authorization code, the
``/token`` exchange performed by the production :class:`HttpxOidcClient`, the
RS256 ``id_token``, and the JWKS it is verified against. Nothing in the OIDC
path is faked. Two test-only helpers on the IdP (``/test/account`` to choose
the account, ``/test/mint`` to sign a token with the IdP's real private key
with chosen claims, e.g. an expired one) are the only non-protocol surface, and
they still use the real key.

SSRF note: ``assert_public_http_url`` blocks loopback, so these tests set the
documented dev escape hatch ``WEBHOOK_ALLOW_PRIVATE_IPS``. The guard's
production behaviour is unchanged, and
``test_loopback_is_still_blocked_when_the_escape_hatch_is_off`` in this very
module proves the control still works with the flag off.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import uuid
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from sqlalchemy import select

from app.api.routers import orgs as orgs_router_module
from app.api.routers import sso as sso_router_module
from app.core.config import settings
from app.db.session import get_sessionmaker
from app.main import app
from app.models.enterprise import SsoConnection
from app.models.org import OrganizationMember
from app.models.user import Role, User
from app.services.sso import (
    HttpxOidcClient,
    SsoError,
    _key_for,
    assert_public_http_url,
    get_oidc_client,
    get_secret_resolver,
    verify_id_token,
)
from tests.helpers import auth_headers, grant_role, register_verify_login

_MOUNTED_PATHS = {getattr(r, "path", None) for r in app.routes}
if "/orgs" not in _MOUNTED_PATHS:
    app.include_router(orgs_router_module.router)
if "/sso/oidc/callback" not in _MOUNTED_PATHS:
    app.include_router(sso_router_module.router)


OPT_IN_ENV = "SSO_OIDC_INTEGRATION"
IDP_BASE = os.environ.get("SSO_IDP_BASE_URL", "http://127.0.0.1:9443").rstrip("/")
IDP_CLIENT_ID = os.environ.get("IDP_CLIENT_ID", "cc-enterprise-client")
IDP_CLIENT_ID_BASIC = os.environ.get(
    "IDP_CLIENT_ID_BASIC", "cc-enterprise-client-basic"
)
IDP_CLIENT_SECRET = os.environ.get(
    "IDP_CLIENT_SECRET", "integration-client-secret-value-0123456789"
)
SECRET_REF = "acme-real-idp"
REDIRECT_URI = f"{IDP_BASE}/cb"

_START_HINT = (
    f"the real OIDC provider is not answering at {IDP_BASE}. Start it with: "
    "cd apps/api/tests/oidc_idp && npm install && PORT=9443 node server.mjs"
)


def _probe() -> dict[str, Any] | None:
    """Fetch real discovery. ``None`` means the IdP is not up."""
    try:
        with httpx.Client(timeout=3.0) as c:
            resp = c.get(f"{IDP_BASE}/.well-known/openid-configuration")
            if resp.status_code != 200:
                return None
            doc = resp.json()
    except Exception:
        return None
    if not doc.get("issuer") or not doc.get("token_endpoint"):
        return None
    return doc


@pytest.fixture(scope="session")
def discovery() -> dict[str, Any]:
    opted_in = os.environ.get(OPT_IN_ENV) not in (None, "", "0")
    doc = _probe()
    if doc is None:
        if opted_in:
            # Opted in and unreachable => FAIL. Never a silent skip.
            pytest.fail(f"{OPT_IN_ENV}=1 was set but {_START_HINT}")
        pytest.skip(f"set {OPT_IN_ENV}=1 and start the real IdP: {_START_HINT}")
    if not opted_in:
        pytest.skip(
            f"integration test: set {OPT_IN_ENV}=1 to run it (the IdP at {IDP_BASE} is up)"
        )
    return doc


class StaticResolver:
    def resolve(self, ref: str) -> str | None:
        return IDP_CLIENT_SECRET if ref == SECRET_REF else None


@pytest_asyncio.fixture
async def real_idp(discovery, monkeypatch):
    """Point the app at the live IdP and use the REAL HttpxOidcClient."""
    monkeypatch.setattr(settings, "SSO_ENABLED", True)
    # Documented dev escape hatch, needed only because the IdP is on loopback.
    # See test_loopback_is_still_blocked_when_the_escape_hatch_is_off.
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_PRIVATE_IPS", True)
    # NOT overridden: get_oidc_client. The production HttpxOidcClient does the
    # real HTTP. That is the entire point of this module.
    app.dependency_overrides.pop(get_oidc_client, None)
    app.dependency_overrides[get_secret_resolver] = lambda: StaticResolver()
    await _set_account(email="sso-real@example.com")
    yield discovery
    app.dependency_overrides.pop(get_secret_resolver, None)


# ---------------------------------------------------------------------------
# IdP control + real protocol helpers
# ---------------------------------------------------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _hs256_with_secret(*, secret: bytes, header: dict[str, Any], payload: dict[str, Any]) -> str:
    """Hand-roll an HS256 JWT with an arbitrary shared secret.

    PyJWT blocks PEM input to HMAC, but that is a signing-side safety net in
    *our* process; an attacker is not using our PyJWT. What is under test is
    whether OUR verification path rejects the forgery.
    """
    signing_input = (
        f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    )
    sig = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"


async def _set_account(*, email: str, sub: str | None = None, verified: bool = True) -> None:
    body: dict[str, Any] = {"email": email, "email_verified": verified}
    if sub:
        body["sub"] = sub
    async with httpx.AsyncClient(timeout=5.0) as c:
        resp = await c.post(f"{IDP_BASE}/test/account", json=body)
    assert resp.status_code == 200, resp.text


async def _mint_with_real_key(**claim_overrides: Any) -> str:
    """Have the IdP sign a token with its REAL RSA private key."""
    async with httpx.AsyncClient(timeout=5.0) as c:
        resp = await c.post(f"{IDP_BASE}/test/mint", json={"claims": claim_overrides})
    assert resp.status_code == 200, resp.text
    return resp.json()["id_token"]


async def _real_jwks() -> dict[str, Any]:
    """Fetch the JWKS through the production client, over real HTTP."""
    return await HttpxOidcClient(timeout=5.0).fetch_jwks(f"{IDP_BASE}/jwks")


async def _follow_authorize(authorization_url: str) -> str:
    """Drive the real /authorize and read the real authorization code.

    A fresh client per call means no IdP session cookie is reused, so every
    login is a complete, fresh authorization.
    """
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
        resp = await c.get(authorization_url)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:400]}"
    payload = resp.json()
    assert "code" in payload, f"IdP returned no code: {payload}"
    return payload["code"]


async def _login(client, email: str) -> str:
    return (await register_verify_login(client, email))["access_token"]


async def _create_org(client, token: str, name: str) -> str:
    resp = await client.post("/orgs", json={"name": name}, headers=auth_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _connection_payload(discovery: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": "oidc",
        "issuer": discovery["issuer"],
        "client_id": IDP_CLIENT_ID,
        "client_secret_ref": SECRET_REF,
        "authorization_endpoint": discovery["authorization_endpoint"],
        "token_endpoint": discovery["token_endpoint"],
        "jwks_uri": discovery["jwks_uri"],
        "redirect_uri": REDIRECT_URI,
        "scopes": ["openid", "email"],
    }


async def _setup_connection(client, token: str, org_id: str, discovery) -> str:
    resp = await client.post(
        f"/sso/orgs/{org_id}/connections",
        json=_connection_payload(discovery),
        headers=auth_headers(token),
    )
    assert resp.status_code == 201, resp.text
    assert IDP_CLIENT_SECRET not in resp.text
    return resp.json()["id"]


async def _start(client, conn_id: str) -> tuple[str, str]:
    """Our build_authorization_url -> (state, real authorization code)."""
    start = await client.post(f"/sso/oidc/{conn_id}/authorize")
    assert start.status_code == 200, start.text
    body = start.json()
    code = await _follow_authorize(body["authorization_url"])
    return body["state"], code


async def _full_login(client, conn_id: str):
    state, code = await _start(client, conn_id)
    return await client.post("/sso/oidc/callback", json={"state": state, "code": code})


async def _exchange_directly(code: str) -> str:
    """Real /token call via the production client, returning a real id_token."""
    token_response = await HttpxOidcClient(timeout=10.0).exchange_code(
        f"{IDP_BASE}/token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": IDP_CLIENT_ID,
            "client_secret": IDP_CLIENT_SECRET,
        },
    )
    return token_response["id_token"]


async def _set_connection_issuer(conn_id: str, issuer: str) -> None:
    sm = get_sessionmaker()
    async with sm() as s:
        row = (
            await s.execute(select(SsoConnection).where(SsoConnection.id == uuid.UUID(conn_id)))
        ).scalar_one()
        row.issuer = issuer
        await s.commit()


# ---------------------------------------------------------------------------
# 1. Happy path: a genuine authorization-code round trip
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_real_authorization_code_round_trip_provisions_and_logs_in(client, real_idp):
    discovery = real_idp
    owner = await _login(client, "rt-owner@example.com")
    org_id = await _create_org(client, owner, "Real IdP Corp")
    conn_id = await _setup_connection(client, owner, org_id, discovery)

    cb = await _full_login(client, conn_id)
    assert cb.status_code == 200, cb.text
    body = cb.json()
    assert body["email"] == "sso-real@example.com"
    assert body["provisioned"] is True
    assert body["org_id"] == org_id

    # The minted session is a real one.
    me = await client.get("/orgs", headers=auth_headers(body["access_token"]))
    assert me.status_code == 200
    assert [o["id"] for o in me.json()] == [org_id]

    sm = get_sessionmaker()
    async with sm() as s:
        membership = (
            await s.execute(
                select(OrganizationMember).where(
                    OrganizationMember.org_id == uuid.UUID(org_id),
                    OrganizationMember.user_id == uuid.UUID(body["user_id"]),
                )
            )
        ).scalar_one()
        assert membership.role == "member"
        user = (
            await s.execute(select(User).where(User.id == uuid.UUID(body["user_id"])))
        ).scalar_one()
        assert user.email_verified is True

    # Second login reuses the identity rather than provisioning again.
    cb2 = await _full_login(client, conn_id)
    assert cb2.status_code == 200, cb2.text
    assert cb2.json()["provisioned"] is False
    assert cb2.json()["user_id"] == body["user_id"]


@pytest.mark.asyncio
async def test_id_token_really_is_rs256_signed_by_the_real_jwks(client, real_idp):
    """Guards against the suite passing on an HS256 shortcut."""
    discovery = real_idp
    owner = await _login(client, "rs256-owner@example.com")
    org_id = await _create_org(client, owner, "RS256 Corp")
    conn_id = await _setup_connection(client, owner, org_id, discovery)

    _state, code = await _start(client, conn_id)
    id_token = await _exchange_directly(code)

    header = jwt.get_unverified_header(id_token)
    assert header["alg"] == "RS256"
    jwks = await _real_jwks()
    kids = {k["kid"]: k for k in jwks["keys"]}
    assert header["kid"] in kids
    assert kids[header["kid"]]["kty"] == "RSA"


# ---------------------------------------------------------------------------
# 2. Signature tampering
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tampered_signature_is_rejected(client, real_idp):
    discovery = real_idp
    owner = await _login(client, "tamper-owner@example.com")
    org_id = await _create_org(client, owner, "Tamper Corp")
    conn_id = await _setup_connection(client, owner, org_id, discovery)

    _state, code = await _start(client, conn_id)
    id_token = await _exchange_directly(code)
    head, payload, sig = id_token.split(".")
    # Flip a byte in the MIDDLE of the decoded signature, not a base64url
    # character at the end. The final character of an unpadded group encodes
    # only the leftover bits, so several distinct characters decode to the SAME
    # bytes -- an end-character flip therefore produced a byte-identical
    # signature whenever the real one ended in such a character, the token
    # verified, and this test failed intermittently.
    raw = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    mid = len(raw) // 2
    mutated = raw[:mid] + bytes([raw[mid] ^ 0xFF]) + raw[mid + 1 :]
    assert mutated != raw, "mutation did not change the signature bytes"
    tampered_sig = base64.urlsafe_b64encode(mutated).rstrip(b"=").decode()
    tampered = f"{head}.{payload}.{tampered_sig}"

    jwks = await _real_jwks()
    with pytest.raises(SsoError) as exc:
        verify_id_token(
            id_token=tampered,
            jwks=jwks,
            client_secret=IDP_CLIENT_SECRET,
            issuer=discovery["issuer"],
            client_id=IDP_CLIENT_ID,
            nonce=jwt.decode(id_token, options={"verify_signature": False})["nonce"],
        )
    assert exc.value.detail == "sso_invalid_id_token"

    # Sanity: the untampered token verifies, so the rejection above is about
    # the signature and nothing else.
    claims = verify_id_token(
        id_token=id_token,
        jwks=jwks,
        client_secret=IDP_CLIENT_SECRET,
        issuer=discovery["issuer"],
        client_id=IDP_CLIENT_ID,
        nonce=jwt.decode(id_token, options={"verify_signature": False})["nonce"],
    )
    assert claims["email"] == "sso-real@example.com"


# ---------------------------------------------------------------------------
# 3. Algorithm confusion against a REAL RSA JWKS (CRITICAL audit finding)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_algorithm_confusion_against_real_rsa_key_is_rejected(real_idp):
    """Sign HS256 using the IdP's real RSA *public* key as the shared secret.

    The public key is public. If `_key_for` let the attacker-controlled JOSE
    header pick the algorithm, this token would verify and an attacker could
    forge arbitrary id_tokens for this IdP.
    """
    jwks = await _real_jwks()
    rsa_jwk = next(k for k in jwks["keys"] if k["kty"] == "RSA")
    public_key = jwt.PyJWK.from_dict({**rsa_jwk, "alg": "RS256"}).key
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    now = dt.datetime.now(dt.timezone.utc)
    # PyJWT refuses to *create* this token (it rejects PEM input to HMAC), so
    # the attacker's half is hand-rolled, as a real attacker's would be.
    forged = _hs256_with_secret(
        secret=public_pem,
        header={"alg": "HS256", "kid": rsa_jwk["kid"], "typ": "JWT"},
        payload={
            "iss": IDP_BASE,
            "aud": IDP_CLIENT_ID,
            "sub": "attacker",
            "email": "attacker@example.com",
            "email_verified": True,
            "nonce": "n",
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
        },
    )

    with pytest.raises(SsoError) as exc:
        _key_for(jwks, forged, IDP_CLIENT_SECRET)
    assert exc.value.detail == "sso_id_token_alg_not_allowed"

    # And the same through the full verification entry point.
    with pytest.raises(SsoError) as exc2:
        verify_id_token(
            id_token=forged,
            jwks=jwks,
            client_secret=IDP_CLIENT_SECRET,
            issuer=IDP_BASE,
            client_id=IDP_CLIENT_ID,
            nonce="n",
        )
    assert exc2.value.detail == "sso_id_token_alg_not_allowed"


# ---------------------------------------------------------------------------
# 4. Issuer / audience / nonce / expiry
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wrong_issuer_is_rejected_end_to_end(client, real_idp):
    """A real token from the real IdP, against a connection expecting another."""
    discovery = real_idp
    owner = await _login(client, "iss-owner@example.com")
    org_id = await _create_org(client, owner, "Issuer Corp")
    conn_id = await _setup_connection(client, owner, org_id, discovery)

    state, code = await _start(client, conn_id)
    # Only verification is affected; the token exchange still uses the real
    # token_endpoint from the connection config.
    await _set_connection_issuer(conn_id, "https://someone-elses-idp.example.com")

    cb = await client.post("/sso/oidc/callback", json={"state": state, "code": code})
    assert cb.status_code == 400, cb.text
    assert cb.json()["detail"] == "sso_id_token_issuer_mismatch"


@pytest.mark.asyncio
async def test_wrong_audience_is_rejected(client, real_idp):
    discovery = real_idp
    owner = await _login(client, "aud-owner@example.com")
    org_id = await _create_org(client, owner, "Audience Corp")
    conn_id = await _setup_connection(client, owner, org_id, discovery)

    _state, code = await _start(client, conn_id)
    id_token = await _exchange_directly(code)
    jwks = await _real_jwks()

    with pytest.raises(SsoError) as exc:
        verify_id_token(
            id_token=id_token,
            jwks=jwks,
            client_secret=IDP_CLIENT_SECRET,
            issuer=discovery["issuer"],
            client_id="a-different-registered-client",
            nonce=jwt.decode(id_token, options={"verify_signature": False})["nonce"],
        )
    assert exc.value.detail == "sso_id_token_audience_mismatch"


@pytest.mark.asyncio
async def test_mismatched_nonce_is_rejected_end_to_end(client, real_idp):
    """A real code bound to authorization request #1, replayed under state #2."""
    discovery = real_idp
    owner = await _login(client, "nonce-owner@example.com")
    org_id = await _create_org(client, owner, "Nonce Corp")
    conn_id = await _setup_connection(client, owner, org_id, discovery)

    _state1, code1 = await _start(client, conn_id)
    state2, _code2 = await _start(client, conn_id)

    cb = await client.post("/sso/oidc/callback", json={"state": state2, "code": code1})
    assert cb.status_code == 400, cb.text
    assert cb.json()["detail"] == "sso_nonce_mismatch"


@pytest.mark.asyncio
async def test_replayed_authorization_code_is_rejected_by_the_real_idp(client, real_idp):
    discovery = real_idp
    owner = await _login(client, "replay-owner@example.com")
    org_id = await _create_org(client, owner, "Replay Corp")
    conn_id = await _setup_connection(client, owner, org_id, discovery)

    state, code = await _start(client, conn_id)
    first = await client.post("/sso/oidc/callback", json={"state": state, "code": code})
    assert first.status_code == 200, first.text

    replay = await client.post("/sso/oidc/callback", json={"state": state, "code": code})
    assert replay.status_code == 400, replay.text
    assert replay.json()["detail"] == "sso_token_exchange_failed"


@pytest.mark.asyncio
async def test_expired_id_token_is_rejected(real_idp):
    """Signed with the IdP's real RSA private key, just already expired."""
    discovery = real_idp
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    id_token = await _mint_with_real_key(iat=now - 7200, exp=now - 3600, nonce="n")
    jwks = await _real_jwks()

    with pytest.raises(SsoError) as exc:
        verify_id_token(
            id_token=id_token,
            jwks=jwks,
            client_secret=IDP_CLIENT_SECRET,
            issuer=discovery["issuer"],
            client_id=IDP_CLIENT_ID,
            nonce="n",
        )
    assert exc.value.detail == "sso_id_token_expired"


# ---------------------------------------------------------------------------
# 5. Org binding still holds against a real IdP
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_real_idp_cannot_adopt_an_account_outside_the_org(client, real_idp):
    discovery = real_idp
    victim_email = "real-victim@example.com"
    assert await _login(client, victim_email)

    attacker = await _login(client, "real-attacker@example.com")
    org_id = await _create_org(client, attacker, "Evil Real Inc")
    conn_id = await _setup_connection(client, attacker, org_id, discovery)

    await _set_account(email=victim_email, sub="victim-subject")
    cb = await _full_login(client, conn_id)
    assert cb.status_code == 403, cb.text
    assert cb.json()["detail"] == "sso_identity_not_linked"


@pytest.mark.asyncio
async def test_real_idp_cannot_federate_a_platform_administrator(client, real_idp):
    discovery = real_idp
    admin_email = "real-admin@example.com"
    admin = await _login(client, admin_email)
    await grant_role(admin_email, Role.administrator)

    attacker = await _login(client, "real-attacker2@example.com")
    org_id = await _create_org(client, attacker, "Evil Real Inc 2")
    invite = await client.post(
        f"/orgs/{org_id}/invitations",
        json={"email": admin_email, "role": "member"},
        headers=auth_headers(attacker),
    )
    assert invite.status_code == 201, invite.text
    accepted = await client.post(
        "/orgs/invitations/accept",
        json={"token": invite.json()["token"]},
        headers=auth_headers(admin),
    )
    assert accepted.status_code == 200, accepted.text

    conn_id = await _setup_connection(client, attacker, org_id, discovery)
    await _set_account(email=admin_email, sub="admin-subject")
    cb = await _full_login(client, conn_id)
    assert cb.status_code == 403, cb.text
    assert cb.json()["detail"] == "sso_privileged_account_refused"


# ---------------------------------------------------------------------------
# 6. The SSRF control we temporarily relax is still a control
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_loopback_is_still_blocked_when_the_escape_hatch_is_off(real_idp, monkeypatch):
    """WEBHOOK_ALLOW_PRIVATE_IPS is the only reason loopback is reachable above."""
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_PRIVATE_IPS", False)
    for url in (
        f"{IDP_BASE}/token",
        f"{IDP_BASE}/jwks",
        "http://169.254.169.254/latest/meta-data/",
    ):
        with pytest.raises(SsoError) as exc:
            assert_public_http_url(url)
        assert exc.value.detail == "sso_endpoint_private_address", url

    with pytest.raises(SsoError) as exc:
        assert_public_http_url("file:///etc/passwd")
    assert exc.value.detail == "sso_endpoint_scheme_not_allowed"

    # And the production client refuses to talk to the live loopback IdP.
    with pytest.raises(SsoError):
        await HttpxOidcClient(timeout=5.0).fetch_jwks(f"{IDP_BASE}/jwks")


@pytest.mark.asyncio
async def test_client_secret_basic_round_trip_against_the_real_idp(client, real_idp):
    """The OAuth 2.0 DEFAULT client auth method must work end to end.

    HttpxOidcClient only ever posted credentials in the body
    (client_secret_post). Against an IdP registered for client_secret_basic --
    the spec default, and all some providers accept -- the exchange failed with
    an opaque sso_token_exchange_failed and SSO simply could not be used.

    The IdP enforces the method PER CLIENT, so this only passes if we really do
    send a correctly-encoded Authorization: Basic header.
    """
    discovery = real_idp
    owner = await _login(client, "basic-owner@example.com")
    org_id = await _create_org(client, owner, "Basic Auth Corp")

    payload = _connection_payload(discovery)
    payload["client_id"] = IDP_CLIENT_ID_BASIC
    payload["token_endpoint_auth_method"] = "client_secret_basic"
    resp = await client.post(
        f"/sso/orgs/{org_id}/connections",
        json=payload,
        headers=auth_headers(owner),
    )
    assert resp.status_code == 201, resp.text
    conn_id = resp.json()["id"]

    await _set_account(email="basic-sso@example.com", sub="basic-subject")
    cb = await _full_login(client, conn_id)
    assert cb.status_code == 200, cb.text
    assert cb.json()["email"] == "basic-sso@example.com"


# NOTE: node-oidc-provider accepts a body-posted secret even for a client
# registered as client_secret_basic (verified by hand: a wrong secret gives
# invalid_client, the right one in either position authenticates). So the round
# trip above proves INTEROP, not that we really sent a Basic header -- it would
# pass even with the feature reverted. The wire-level proof that the header is
# built and encoded correctly lives in test_c2_orgs_sso.py, which runs always.
