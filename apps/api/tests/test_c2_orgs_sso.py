"""Phase C2: organizations, invitations, org RBAC and enterprise SSO.

The SSO tests run entirely offline against a mock IdP injected through the
``get_oidc_client`` dependency. That proves *our* half of the OIDC
authorization-code flow (state signature, nonce binding, issuer/audience and
signature checks, JIT provisioning). Interoperability with a real, certified
IdP is covered separately by ``tests/test_c2_sso_oidc_integration.py`` (opt-in:
``SSO_OIDC_INTEGRATION=1``), which drives this same code against a live
node-oidc-provider instance.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from typing import Any

import jwt
import pytest
from sqlalchemy import select

from app.api.routers import orgs as orgs_router_module
from app.api.routers import sso as sso_router_module
from app.core.config import settings
from app.core.security import hash_token
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.main import app
from app.models.enterprise import OrganizationInvitation, SsoConnection
from app.models.org import OrganizationMember, Project
from app.models.user import Role, User
from app.services.sso import get_oidc_client, get_secret_resolver
from tests.helpers import auth_headers, grant_role, register_verify_login

# The Integrator wires these into app/main.py; mounting them here (idempotently)
# keeps this suite green both before and after that happens.
_MOUNTED_PATHS = {getattr(r, "path", None) for r in app.routes}
if "/orgs" not in _MOUNTED_PATHS:
    app.include_router(orgs_router_module.router)
if "/sso/oidc/callback" not in _MOUNTED_PATHS:
    app.include_router(sso_router_module.router)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _login(client, email: str) -> str:
    tokens = await register_verify_login(client, email)
    return tokens["access_token"]


async def _create_org(client, token: str, name: str = "Acme") -> str:
    resp = await client.post("/orgs", json={"name": name}, headers=auth_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _invite(client, token: str, org_id: str, email: str, role: str = "member") -> dict:
    resp = await client.post(
        f"/orgs/{org_id}/invitations",
        json={"email": email, "role": role},
        headers=auth_headers(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _accept(client, token: str, raw_token: str):
    return await client.post(
        "/orgs/invitations/accept",
        json={"token": raw_token},
        headers=auth_headers(token),
    )


async def _user_id(email: str) -> uuid.UUID:
    sm = get_sessionmaker()
    async with sm() as s:
        return (
            await s.execute(select(User).where(User.email == email.lower()))
        ).scalar_one().id


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invite_accept_creates_membership_and_token_is_hashed(client):
    owner = await _login(client, "c2-owner@example.com")
    org_id = await _create_org(client, owner)
    invite = await _invite(client, owner, org_id, "c2-invitee@example.com")
    raw = invite["token"]

    # Only the hash is persisted.
    sm = get_sessionmaker()
    async with sm() as s:
        row = (
            await s.execute(
                select(OrganizationInvitation).where(
                    OrganizationInvitation.id == uuid.UUID(invite["id"])
                )
            )
        ).scalar_one()
        assert row.token_hash == hash_token(raw)
        assert raw not in row.token_hash

    # Listing invitations never leaks the token.
    listed = await client.get(
        f"/orgs/{org_id}/invitations", headers=auth_headers(owner)
    )
    assert listed.status_code == 200
    assert all("token" not in i and "token_hash" not in i for i in listed.json())

    invitee = await _login(client, "c2-invitee@example.com")
    # Before accepting, the invitee is an outsider: 404, not 403.
    assert (await client.get(f"/orgs/{org_id}", headers=auth_headers(invitee))).status_code == 404

    accepted = await _accept(client, invitee, raw)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["role"] == "member"

    assert (await client.get(f"/orgs/{org_id}", headers=auth_headers(invitee))).status_code == 200
    members = (await client.get(f"/orgs/{org_id}/members", headers=auth_headers(owner))).json()
    assert len(members) == 2


@pytest.mark.asyncio
async def test_used_expired_revoked_and_forged_tokens_are_rejected(client):
    owner = await _login(client, "c2-owner2@example.com")
    org_id = await _create_org(client, owner)
    invitee = await _login(client, "c2-invitee2@example.com")

    # 1. forged token
    forged = await _accept(client, invitee, "totally-made-up-token")
    assert forged.status_code == 400
    assert forged.json()["detail"] == "invalid_or_expired_invitation"

    # 2. single use: the second redemption fails
    raw = (await _invite(client, owner, org_id, "c2-invitee2@example.com"))["token"]
    assert (await _accept(client, invitee, raw)).status_code == 200
    replay = await _accept(client, invitee, raw)
    assert replay.status_code == 400

    # 3. expired token
    other = await _login(client, "c2-invitee3@example.com")
    raw_exp = (await _invite(client, owner, org_id, "c2-invitee3@example.com"))["token"]
    sm = get_sessionmaker()
    async with sm() as s:
        row = (
            await s.execute(
                select(OrganizationInvitation).where(
                    OrganizationInvitation.token_hash == hash_token(raw_exp)
                )
            )
        ).scalar_one()
        row.expires_at = utcnow() - dt.timedelta(minutes=1)
        await s.commit()
    expired = await _accept(client, other, raw_exp)
    assert expired.status_code == 400

    # 4. revoked token
    fourth = await _login(client, "c2-invitee4@example.com")
    invite = await _invite(client, owner, org_id, "c2-invitee4@example.com")
    revoke = await client.delete(
        f"/orgs/{org_id}/invitations/{invite['id']}", headers=auth_headers(owner)
    )
    assert revoke.status_code == 204
    assert (await _accept(client, fourth, invite["token"])).status_code == 400

    # 5. a valid token issued to someone else's mailbox
    fifth_raw = (await _invite(client, owner, org_id, "c2-someone-else@example.com"))["token"]
    stranger = await _login(client, "c2-stranger@example.com")
    mismatch = await _accept(client, stranger, fifth_raw)
    assert mismatch.status_code == 403
    assert mismatch.json()["detail"] == "invitation_email_mismatch"


@pytest.mark.asyncio
async def test_reinvite_does_not_duplicate_membership(client):
    owner = await _login(client, "c2-owner3@example.com")
    org_id = await _create_org(client, owner)
    invitee_email = "c2-dup@example.com"
    raw1 = (await _invite(client, owner, org_id, invitee_email))["token"]
    invitee = await _login(client, invitee_email)
    assert (await _accept(client, invitee, raw1)).status_code == 200

    raw2 = (await _invite(client, owner, org_id, invitee_email, role="admin"))["token"]
    assert (await _accept(client, invitee, raw2)).status_code == 200

    sm = get_sessionmaker()
    async with sm() as s:
        rows = (
            await s.execute(
                select(OrganizationMember).where(
                    OrganizationMember.org_id == uuid.UUID(org_id),
                    OrganizationMember.user_id == await _user_id(invitee_email),
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    # Promotion by a legitimate re-invite is allowed; duplication is not.
    assert rows[0].role == "admin"

    # A later `member` invite must not silently demote the admin.
    raw3 = (await _invite(client, owner, org_id, invitee_email, role="member"))["token"]
    assert (await _accept(client, invitee, raw3)).status_code == 200
    async with sm() as s:
        row = (
            await s.execute(
                select(OrganizationMember).where(
                    OrganizationMember.org_id == uuid.UUID(org_id),
                    OrganizationMember.user_id == await _user_id(invitee_email),
                )
            )
        ).scalar_one()
    assert row.role == "admin"


# ---------------------------------------------------------------------------
# Org isolation & roles
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_non_member_gets_404_on_org_and_org_project(client):
    owner = await _login(client, "c2-owner4@example.com")
    org_id = await _create_org(client, owner, name="Private Co")
    proj = await client.post(
        "/projects", json={"name": "shared", "org_id": org_id}, headers=auth_headers(owner)
    )
    assert proj.status_code == 201, proj.text
    project_id = proj.json()["id"]

    outsider = await _login(client, "c2-outsider@example.com")
    assert (await client.get(f"/orgs/{org_id}", headers=auth_headers(outsider))).status_code == 404
    assert (
        await client.get(f"/orgs/{org_id}/members", headers=auth_headers(outsider))
    ).status_code == 404
    assert (
        await client.get(f"/orgs/{org_id}/projects", headers=auth_headers(outsider))
    ).status_code == 404
    assert (
        await client.get(f"/projects/{project_id}", headers=auth_headers(outsider))
    ).status_code == 404

    # A member sees the shared project.
    raw = (await _invite(client, owner, org_id, "c2-teammate@example.com"))["token"]
    mate = await _login(client, "c2-teammate@example.com")
    assert (await _accept(client, mate, raw)).status_code == 200
    shared = await client.get(f"/orgs/{org_id}/projects", headers=auth_headers(mate))
    assert shared.status_code == 200
    assert [p["id"] for p in shared.json()] == [project_id]
    assert (
        await client.get(f"/projects/{project_id}", headers=auth_headers(mate))
    ).status_code == 200


@pytest.mark.asyncio
async def test_member_cannot_self_escalate_or_promote_others(client):
    owner = await _login(client, "c2-owner5@example.com")
    org_id = await _create_org(client, owner)
    raw = (await _invite(client, owner, org_id, "c2-plain@example.com"))["token"]
    member = await _login(client, "c2-plain@example.com")
    assert (await _accept(client, member, raw)).status_code == 200
    member_user_id = str(await _user_id("c2-plain@example.com"))

    escalate = await client.patch(
        f"/orgs/{org_id}/members/{member_user_id}",
        json={"role": "owner"},
        headers=auth_headers(member),
    )
    assert escalate.status_code == 403
    admin_try = await client.patch(
        f"/orgs/{org_id}/members/{member_user_id}",
        json={"role": "admin"},
        headers=auth_headers(member),
    )
    assert admin_try.status_code == 403
    # A member may not rename the org either.
    assert (
        await client.patch(
            f"/orgs/{org_id}", json={"name": "Hijacked"}, headers=auth_headers(member)
        )
    ).status_code == 403

    # The role really did not change.
    members = (await client.get(f"/orgs/{org_id}/members", headers=auth_headers(owner))).json()
    roles = {m["user_id"]: m["role"] for m in members}
    assert roles[member_user_id] == "member"

    # An admin cannot mint an owner.
    promote = await client.patch(
        f"/orgs/{org_id}/members/{member_user_id}",
        json={"role": "admin"},
        headers=auth_headers(owner),
    )
    assert promote.status_code == 200 and promote.json()["role"] == "admin"
    mint_owner = await client.patch(
        f"/orgs/{org_id}/members/{member_user_id}",
        json={"role": "owner"},
        headers=auth_headers(member),
    )
    assert mint_owner.status_code == 403


@pytest.mark.asyncio
async def test_last_owner_cannot_be_demoted_removed_or_leave(client):
    owner_email = "c2-owner6@example.com"
    owner = await _login(client, owner_email)
    org_id = await _create_org(client, owner)
    owner_id = str(await _user_id(owner_email))

    demote = await client.patch(
        f"/orgs/{org_id}/members/{owner_id}",
        json={"role": "admin"},
        headers=auth_headers(owner),
    )
    assert demote.status_code == 409, demote.text
    remove = await client.delete(
        f"/orgs/{org_id}/members/{owner_id}", headers=auth_headers(owner)
    )
    assert remove.status_code == 409
    leave = await client.post(f"/orgs/{org_id}/leave", headers=auth_headers(owner))
    assert leave.status_code == 409

    # With a second owner in place, the first may step down.
    raw = (await _invite(client, owner, org_id, "c2-coowner@example.com", role="owner"))["token"]
    co = await _login(client, "c2-coowner@example.com")
    assert (await _accept(client, co, raw)).status_code == 200
    assert (await client.post(f"/orgs/{org_id}/leave", headers=auth_headers(owner))).status_code == 204


# ---------------------------------------------------------------------------
# SSO — mock IdP
# ---------------------------------------------------------------------------
IDP_ISSUER = "https://idp.example.com"
IDP_CLIENT_ID = "cc-enterprise-client"
IDP_SECRET = "s3cr3t-client-secret-value-for-tests"
SECRET_REF = "acme-okta"


class FakeIdp:
    """Mock OIDC provider: HS256-signed id_tokens + a matching oct JWKS."""

    def __init__(self) -> None:
        self.issuer = IDP_ISSUER
        self.audience = IDP_CLIENT_ID
        self.email = "sso-user@example.com"
        self.email_verified = True
        self.nonce_override: str | None = None
        self.exchange_calls: list[dict[str, str]] = []

    def _jwk(self) -> dict[str, Any]:
        return {
            "kty": "oct",
            "kid": "mock-key-1",
            "alg": "HS256",
            "k": base64.urlsafe_b64encode(IDP_SECRET.encode()).decode().rstrip("="),
        }

    def mint(self, nonce: str) -> str:
        now = dt.datetime.now(dt.timezone.utc)
        return jwt.encode(
            {
                "iss": self.issuer,
                "aud": self.audience,
                "sub": "idp-subject-123",
                "email": self.email,
                "email_verified": self.email_verified,
                "nonce": self.nonce_override or nonce,
                "iat": int(now.timestamp()),
                "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
            },
            IDP_SECRET,
            algorithm="HS256",
            headers={"kid": "mock-key-1"},
        )

    # --- OidcClient protocol ------------------------------------------------
    async def exchange_code(self, token_endpoint: str, data: dict[str, str]) -> dict[str, Any]:
        self.exchange_calls.append(dict(data))
        assert data["grant_type"] == "authorization_code"
        assert data["client_secret"] == IDP_SECRET, "resolver must supply the live secret"
        nonce = self._pending_nonce
        return {"id_token": self.mint(nonce), "access_token": "idp-access", "token_type": "bearer"}

    async def fetch_jwks(self, jwks_uri: str) -> dict[str, Any]:
        return {"keys": [self._jwk()]}

    async def discover(self, issuer: str) -> dict[str, Any]:
        return {"issuer": self.issuer}

    _pending_nonce: str = ""


class StaticResolver:
    def resolve(self, ref: str) -> str | None:
        return IDP_SECRET if ref == SECRET_REF else None


@pytest.fixture
def sso_env(monkeypatch):
    """Enable SSO and let the suite use non-resolvable example.com endpoints."""
    monkeypatch.setattr(settings, "SSO_ENABLED", True)
    # The SSRF guard resolves hostnames; the offline suite has no DNS, so reuse
    # the deployment's "allow private egress" switch for setup-time validation.
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_PRIVATE_IPS", True)
    idp = FakeIdp()
    resolver = StaticResolver()
    app.dependency_overrides[get_oidc_client] = lambda: idp
    app.dependency_overrides[get_secret_resolver] = lambda: resolver
    yield idp
    app.dependency_overrides.pop(get_oidc_client, None)
    app.dependency_overrides.pop(get_secret_resolver, None)


def _connection_payload() -> dict[str, Any]:
    return {
        "provider": "oidc",
        "issuer": IDP_ISSUER,
        "client_id": IDP_CLIENT_ID,
        "client_secret_ref": SECRET_REF,
        "authorization_endpoint": f"{IDP_ISSUER}/authorize",
        "token_endpoint": f"{IDP_ISSUER}/token",
        "jwks_uri": f"{IDP_ISSUER}/jwks",
        "redirect_uri": "https://app.computecoin.test/sso/callback",
        "scopes": ["openid", "email"],
    }


async def _setup_connection(client, owner_token: str, org_id: str) -> str:
    resp = await client.post(
        f"/sso/orgs/{org_id}/connections",
        json=_connection_payload(),
        headers=auth_headers(owner_token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # The response must never carry a secret, only its reference.
    assert IDP_SECRET not in resp.text
    assert body["client_secret_ref"] == SECRET_REF
    return body["id"]


@pytest.mark.asyncio
async def test_sso_disabled_is_invisible(client):
    owner = await _login(client, "c2-sso-off@example.com")
    org_id = await _create_org(client, owner)
    resp = await client.post(
        f"/sso/orgs/{org_id}/connections",
        json=_connection_payload(),
        headers=auth_headers(owner),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_sso_connection_management_is_owner_admin_only(client, sso_env):
    owner = await _login(client, "c2-sso-owner@example.com")
    org_id = await _create_org(client, owner)
    conn_id = await _setup_connection(client, owner, org_id)

    # The client secret is nowhere in the stored row.
    sm = get_sessionmaker()
    async with sm() as s:
        row = (
            await s.execute(select(SsoConnection).where(SsoConnection.id == uuid.UUID(conn_id)))
        ).scalar_one()
        assert IDP_SECRET not in str(row.config)
        assert row.config["client_secret_ref"] == SECRET_REF
        assert row.config["client_secret_sha256"] != IDP_SECRET

    raw = (await _invite(client, owner, org_id, "c2-sso-member@example.com"))["token"]
    member = await _login(client, "c2-sso-member@example.com")
    assert (await _accept(client, member, raw)).status_code == 200

    # Plain member: 403 (they know the org exists).
    assert (
        await client.get(f"/sso/orgs/{org_id}/connections", headers=auth_headers(member))
    ).status_code == 403
    assert (
        await client.post(
            f"/sso/orgs/{org_id}/connections",
            json=_connection_payload(),
            headers=auth_headers(member),
        )
    ).status_code == 403
    assert (
        await client.delete(
            f"/sso/orgs/{org_id}/connections/{conn_id}", headers=auth_headers(member)
        )
    ).status_code == 403

    # Outsider: 404.
    outsider = await _login(client, "c2-sso-outsider@example.com")
    assert (
        await client.get(f"/sso/orgs/{org_id}/connections", headers=auth_headers(outsider))
    ).status_code == 404

    # Owner can list and delete.
    assert (
        await client.get(f"/sso/orgs/{org_id}/connections", headers=auth_headers(owner))
    ).status_code == 200
    assert (
        await client.delete(
            f"/sso/orgs/{org_id}/connections/{conn_id}", headers=auth_headers(owner)
        )
    ).status_code == 204


@pytest.mark.asyncio
async def test_sso_connection_rejects_ssrf_endpoints(client, sso_env, monkeypatch):
    owner = await _login(client, "c2-ssrf@example.com")
    org_id = await _create_org(client, owner)
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_PRIVATE_IPS", False)

    # Literal public IPs for the other endpoints: the offline suite has no DNS,
    # and we want the guard's verdict, not a resolution failure.
    payload = _connection_payload()
    payload["authorization_endpoint"] = "https://93.184.216.34/authorize"
    payload["jwks_uri"] = "https://93.184.216.34/jwks"
    payload["token_endpoint"] = "http://169.254.169.254/latest/meta-data/"
    resp = await client.post(
        f"/sso/orgs/{org_id}/connections", json=payload, headers=auth_headers(owner)
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "sso_endpoint_private_address"

    payload = _connection_payload()
    payload["authorization_endpoint"] = "https://93.184.216.34/authorize"
    payload["token_endpoint"] = "https://93.184.216.34/token"
    payload["jwks_uri"] = "file:///etc/passwd"
    resp = await client.post(
        f"/sso/orgs/{org_id}/connections", json=payload, headers=auth_headers(owner)
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "sso_endpoint_scheme_not_allowed"


@pytest.mark.asyncio
async def test_oidc_flow_against_mock_idp_provisions_and_logs_in(client, sso_env):
    idp = sso_env
    owner = await _login(client, "c2-sso-owner2@example.com")
    org_id = await _create_org(client, owner, name="SSO Corp")
    conn_id = await _setup_connection(client, owner, org_id)

    start = await client.post(f"/sso/oidc/{conn_id}/authorize")
    assert start.status_code == 200, start.text
    state = start.json()["state"]
    url = start.json()["authorization_url"]
    assert url.startswith(f"{IDP_ISSUER}/authorize?")
    assert f"client_id={IDP_CLIENT_ID}" in url
    assert "nonce=" in url and "state=" in url

    # The IdP echoes the nonce it received in the authorization request.
    idp._pending_nonce = jwt.decode(
        state, settings.SESSION_SECRET, algorithms=["HS256"]
    )["nonce"]

    cb = await client.post("/sso/oidc/callback", json={"state": state, "code": "auth-code-1"})
    assert cb.status_code == 200, cb.text
    body = cb.json()
    assert body["email"] == "sso-user@example.com"
    assert body["provisioned"] is True
    assert body["org_id"] == org_id

    # The minted access token works on a normal authenticated endpoint, and the
    # SSO user is now a member of the org.
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

    # A second login reuses the existing identity instead of provisioning again.
    start2 = await client.post(f"/sso/oidc/{conn_id}/authorize")
    state2 = start2.json()["state"]
    idp._pending_nonce = jwt.decode(
        state2, settings.SESSION_SECRET, algorithms=["HS256"]
    )["nonce"]
    cb2 = await client.post("/sso/oidc/callback", json={"state": state2, "code": "auth-code-2"})
    assert cb2.status_code == 200
    assert cb2.json()["provisioned"] is False
    assert cb2.json()["user_id"] == body["user_id"]


@pytest.mark.asyncio
async def test_oidc_rejects_forged_state_bad_nonce_and_wrong_issuer(client, sso_env):
    idp = sso_env
    owner = await _login(client, "c2-sso-owner3@example.com")
    org_id = await _create_org(client, owner)
    conn_id = await _setup_connection(client, owner, org_id)

    # 1. Forged state: signed with a key we do not trust.
    forged = jwt.encode(
        {
            "purpose": "sso_oidc_state",
            "cid": conn_id,
            "nonce": "attacker-nonce",
            "jti": "x",
            "exp": int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).timestamp()),
        },
        "attacker-key",
        algorithm="HS256",
    )
    resp = await client.post("/sso/oidc/callback", json={"state": forged, "code": "c"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "sso_invalid_state"

    # 2. Expired state.
    expired = jwt.encode(
        {
            "purpose": "sso_oidc_state",
            "cid": conn_id,
            "nonce": "n",
            "jti": "y",
            "exp": int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).timestamp()),
        },
        settings.SESSION_SECRET,
        algorithm="HS256",
    )
    resp = await client.post("/sso/oidc/callback", json={"state": expired, "code": "c"})
    assert resp.status_code == 400 and resp.json()["detail"] == "sso_invalid_state"

    # 3. Replayed id_token minted for a different authorization request.
    state = (await client.post(f"/sso/oidc/{conn_id}/authorize")).json()["state"]
    idp._pending_nonce = "some-other-request-nonce"
    resp = await client.post("/sso/oidc/callback", json={"state": state, "code": "c"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "sso_nonce_mismatch"

    # 4. id_token from the wrong issuer.
    state = (await client.post(f"/sso/oidc/{conn_id}/authorize")).json()["state"]
    idp._pending_nonce = jwt.decode(
        state, settings.SESSION_SECRET, algorithms=["HS256"]
    )["nonce"]
    idp.issuer = "https://evil-idp.example.net"
    resp = await client.post("/sso/oidc/callback", json={"state": state, "code": "c"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "sso_id_token_issuer_mismatch"
    idp.issuer = IDP_ISSUER

    # 5. id_token minted for a different client (audience).
    state = (await client.post(f"/sso/oidc/{conn_id}/authorize")).json()["state"]
    idp._pending_nonce = jwt.decode(
        state, settings.SESSION_SECRET, algorithms=["HS256"]
    )["nonce"]
    idp.audience = "some-other-client"
    resp = await client.post("/sso/oidc/callback", json={"state": state, "code": "c"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "sso_id_token_audience_mismatch"
    idp.audience = IDP_CLIENT_ID

    # 6. An unverified IdP email must not provision an account.
    state = (await client.post(f"/sso/oidc/{conn_id}/authorize")).json()["state"]
    idp._pending_nonce = jwt.decode(
        state, settings.SESSION_SECRET, algorithms=["HS256"]
    )["nonce"]
    idp.email_verified = False
    resp = await client.post("/sso/oidc/callback", json={"state": state, "code": "c"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "sso_email_not_verified"
    idp.email_verified = True

    # Nothing above created a user or a membership.
    sm = get_sessionmaker()
    async with sm() as s:
        assert (
            await s.execute(select(User).where(User.email == "sso-user@example.com"))
        ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_oidc_fails_closed_when_client_secret_is_unavailable(client, sso_env):
    owner = await _login(client, "c2-sso-owner4@example.com")
    org_id = await _create_org(client, owner)
    conn_id = await _setup_connection(client, owner, org_id)
    state = (await client.post(f"/sso/oidc/{conn_id}/authorize")).json()["state"]

    class EmptyResolver:
        def resolve(self, ref: str) -> str | None:
            return None

    app.dependency_overrides[get_secret_resolver] = lambda: EmptyResolver()
    resp = await client.post("/sso/oidc/callback", json={"state": state, "code": "c"})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "sso_client_secret_unavailable"


@pytest.mark.asyncio
async def test_org_project_listing_is_scoped_to_the_org(client):
    """Guard against a listing that leaks another org's projects."""
    owner_a = await _login(client, "c2-a@example.com")
    org_a = await _create_org(client, owner_a, name="A")
    owner_b = await _login(client, "c2-b@example.com")
    org_b = await _create_org(client, owner_b, name="B")
    for token, org in ((owner_a, org_a), (owner_b, org_b)):
        resp = await client.post(
            "/projects", json={"name": f"p-{org}", "org_id": org}, headers=auth_headers(token)
        )
        assert resp.status_code == 201

    listing = await client.get(f"/orgs/{org_a}/projects", headers=auth_headers(owner_a))
    assert listing.status_code == 200
    sm = get_sessionmaker()
    async with sm() as s:
        all_projects = (await s.execute(select(Project))).scalars().all()
    assert len(all_projects) == 2
    assert len(listing.json()) == 1
    assert listing.json()[0]["org_id"] == org_a


@pytest.mark.asyncio
async def test_oidc_rejects_algorithm_confusion(client, sso_env):
    """An RSA verification key must never be usable as an HS256 shared secret.

    This is a unit-level check on the key selector: PyJWT is happy to verify an
    HS256 token whose "secret" is an RSA *public* key, which is public data, so
    the key family — not the attacker-controlled JOSE header — has to decide
    which algorithms are acceptable.
    """
    from app.services.sso import SsoError, _key_for

    rsa_jwk = {"kty": "RSA", "kid": "rsa-1", "n": "0vx7ag", "e": "AQAB"}
    forged = jwt.encode({"sub": "x"}, "any-secret", algorithm="HS256", headers={"kid": "rsa-1"})
    with pytest.raises(SsoError) as excinfo:
        _key_for({"keys": [rsa_jwk]}, forged, "client-secret")
    assert excinfo.value.detail == "sso_id_token_alg_not_allowed"

    # An IdP that pins alg=RS256 on its key is honoured strictly.
    pinned = {"kty": "oct", "kid": "oct-1", "alg": "HS512", "k": "AAAA"}
    tok = jwt.encode({"sub": "x"}, "s", algorithm="HS256", headers={"kid": "oct-1"})
    with pytest.raises(SsoError) as excinfo:
        _key_for({"keys": [pinned]}, tok, "client-secret")
    assert excinfo.value.detail == "sso_id_token_alg_not_allowed"


# ---------------------------------------------------------------------------
# Regression: an attacker-controlled IdP must not take over existing accounts
# ---------------------------------------------------------------------------
async def _complete_callback(client, conn_id: str, idp: FakeIdp):
    start = await client.post(f"/sso/oidc/{conn_id}/authorize")
    assert start.status_code == 200, start.text
    state = start.json()["state"]
    idp._pending_nonce = jwt.decode(
        state, settings.SESSION_SECRET, algorithms=["HS256"]
    )["nonce"]
    return await client.post(
        "/sso/oidc/callback", json={"state": state, "code": "code-1"}
    )


@pytest.mark.asyncio
async def test_oidc_cannot_adopt_an_existing_account_outside_the_org(client, sso_env):
    """A self-registered org + self-registered IdP must not mint a victim's session.

    Any authenticated user can create an org and become its owner, and an org
    admin registers the issuer / client_id / jwks_uri themselves — so
    verify_id_token checks the assertion against values the attacker chose. If
    the callback resolved the asserted email to a pre-existing platform user,
    asserting ``email: victim@…`` would hand the attacker that user's session.
    """
    victim = await _login(client, "c2-sso-victim@example.com")
    assert victim  # the victim account exists and is NOT in the attacker's org

    attacker = await _login(client, "c2-sso-attacker@example.com")
    org_id = await _create_org(client, attacker, name="Evil Inc")
    conn_id = await _setup_connection(client, attacker, org_id)

    idp = sso_env
    idp.email = "c2-sso-victim@example.com"

    cb = await _complete_callback(client, conn_id, idp)
    assert cb.status_code == 403, cb.text
    assert cb.json()["detail"] == "sso_identity_not_linked"


@pytest.mark.asyncio
async def test_oidc_refuses_to_federate_a_platform_administrator(client, sso_env):
    """Even an org member who is a platform admin may not log in via SSO."""
    admin = await _login(client, "c2-sso-admin@example.com")
    await grant_role("c2-sso-admin@example.com", Role.administrator)

    attacker = await _login(client, "c2-sso-attacker2@example.com")
    org_id = await _create_org(client, attacker, name="Evil Inc 2")
    # Put the admin inside the attacker's org, so only the role check can save us.
    raw = (await _invite(client, attacker, org_id, "c2-sso-admin@example.com"))["token"]
    assert (await _accept(client, admin, raw)).status_code == 200

    conn_id = await _setup_connection(client, attacker, org_id)
    idp = sso_env
    idp.email = "c2-sso-admin@example.com"

    cb = await _complete_callback(client, conn_id, idp)
    assert cb.status_code == 403, cb.text
    assert cb.json()["detail"] == "sso_privileged_account_refused"
