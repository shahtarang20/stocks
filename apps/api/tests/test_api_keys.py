"""API key lifecycle: secret shown once, stored hashed, revoke works."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.org import ApiKey
from tests.helpers import auth_headers, register_verify_login


async def _create_project(client, token, name="keyring"):
    resp = await client.post("/projects", json={"name": name}, headers=auth_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_api_key_secret_returned_once_and_stored_hashed(client):
    tokens = await register_verify_login(client, "frida@example.com")
    token = tokens["access_token"]
    project_id = await _create_project(client, token)

    resp = await client.post(
        f"/projects/{project_id}/api-keys",
        json={"name": "ci-key"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    full_key = body["api_key"]
    assert full_key.startswith("cc_live_")
    assert body["prefix"].startswith("cc_live_")

    # The stored row must NOT contain the plaintext secret.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (await session.execute(select(ApiKey))).scalar_one()
        assert row.key_hash != full_key
        assert full_key not in row.key_hash
        assert row.prefix == body["prefix"]
        # No column should hold the raw secret.
        for value in (row.name, row.prefix, row.key_hash):
            assert value != full_key

    # Listing never exposes the secret.
    listed = await client.get(
        f"/projects/{project_id}/api-keys", headers=auth_headers(token)
    )
    assert listed.status_code == 200
    for k in listed.json():
        assert "api_key" not in k
        assert "key_hash" not in k


@pytest.mark.asyncio
async def test_revoked_key_marked_revoked(client):
    tokens = await register_verify_login(client, "gina@example.com")
    token = tokens["access_token"]
    project_id = await _create_project(client, token)

    created = await client.post(
        f"/projects/{project_id}/api-keys",
        json={"name": "temp"},
        headers=auth_headers(token),
    )
    key_id = created.json()["id"]

    revoke = await client.delete(
        f"/projects/{project_id}/api-keys/{key_id}", headers=auth_headers(token)
    )
    assert revoke.status_code == 204

    listed = await client.get(
        f"/projects/{project_id}/api-keys", headers=auth_headers(token)
    )
    key = next(k for k in listed.json() if k["id"] == key_id)
    assert key["revoked_at"] is not None


# ---------------------------------------------------------------------------
# API keys must actually AUTHENTICATE
#
# The lifecycle tests above pass even if a key grants nothing: they only prove
# it is created, hashed and revoked. Nothing consumed an ApiKey, so every key
# ever issued returned 401 while the console advertised them as "programmatic
# access to the ComputeCoin API and OpenAI-compatible inference endpoints".
# ---------------------------------------------------------------------------
def _key_headers(raw_key: str) -> dict:
    return {"Authorization": f"Bearer {raw_key}"}


async def _issue_key(client, token, project_id, name="auth-key") -> str:
    resp = await client.post(
        f"/projects/{project_id}/api-keys",
        json={"name": name},
        headers=auth_headers(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]


@pytest.mark.asyncio
async def test_a_valid_api_key_authenticates(client):
    tokens = await register_verify_login(client, "apikey-auth@example.com")
    token = tokens["access_token"]
    project_id = await _create_project(client, token, "auth-proj")
    raw = await _issue_key(client, token, project_id)

    resp = await client.get("/v1/jobs", headers=_key_headers(raw))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_a_revoked_api_key_is_rejected(client):
    tokens = await register_verify_login(client, "apikey-revoked@example.com")
    token = tokens["access_token"]
    project_id = await _create_project(client, token, "rev-proj")

    created = await client.post(
        f"/projects/{project_id}/api-keys",
        json={"name": "doomed"},
        headers=auth_headers(token),
    )
    raw = created.json()["api_key"]
    key_id = created.json()["id"]
    assert (await client.get("/v1/jobs", headers=_key_headers(raw))).status_code == 200

    revoked = await client.delete(
        f"/projects/{project_id}/api-keys/{key_id}", headers=auth_headers(token)
    )
    assert revoked.status_code in (200, 204), revoked.text

    resp = await client.get("/v1/jobs", headers=_key_headers(raw))
    assert resp.status_code == 401, "a revoked key must stop working immediately"


@pytest.mark.asyncio
async def test_a_forged_api_key_is_rejected(client):
    """Right shape, never issued. Must not authenticate anything."""
    resp = await client.get("/v1/jobs", headers=_key_headers("cc_live_" + "A" * 43))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_suspending_the_owner_kills_their_api_keys(client):
    """Suspension must not leave a live credential behind.

    Otherwise an account banned for abuse keeps full API access through any key
    it issued before the ban.
    """
    from app.models.enums import Role, UserStatus
    from app.db.session import get_sessionmaker
    from app.models.user import User

    tokens = await register_verify_login(client, "apikey-suspended@example.com")
    token = tokens["access_token"]
    project_id = await _create_project(client, token, "susp-proj")
    raw = await _issue_key(client, token, project_id)
    assert (await client.get("/v1/jobs", headers=_key_headers(raw))).status_code == 200

    sm = get_sessionmaker()
    async with sm() as db:
        user = await db.scalar(
            select(User).where(User.email == "apikey-suspended@example.com")
        )
        user.status = UserStatus.suspended
        await db.commit()

    resp = await client.get("/v1/jobs", headers=_key_headers(raw))
    assert resp.status_code == 401, "a suspended owner's key must stop working"


@pytest.mark.asyncio
async def test_api_key_acts_as_its_project_owner_and_nothing_more(client):
    """A key carries the owner's identity, not elevated privilege."""
    tokens = await register_verify_login(client, "apikey-scope@example.com")
    token = tokens["access_token"]
    project_id = await _create_project(client, token, "scope-proj")
    raw = await _issue_key(client, token, project_id)

    me = await client.get("/auth/me", headers=_key_headers(raw))
    assert me.status_code == 200, me.text
    assert me.json()["email"] == "apikey-scope@example.com"
    # Not an administrator just because it is an API key.
    assert "administrator" not in me.json()["roles"]
    admin_only = await client.get("/v1/admin/users", headers=_key_headers(raw))
    assert admin_only.status_code == 403


@pytest.mark.asyncio
async def test_using_a_key_records_last_used_at(client):
    """last_used_at exists on the model; it was never written. It is what makes
    an unused or leaked key visible to its owner."""
    tokens = await register_verify_login(client, "apikey-lastused@example.com")
    token = tokens["access_token"]
    project_id = await _create_project(client, token, "lastused-proj")
    raw = await _issue_key(client, token, project_id)

    sm = get_sessionmaker()
    async with sm() as db:
        before = await db.scalar(select(ApiKey).where(ApiKey.prefix == raw[: len("cc_live_") + 8]))
        assert before.last_used_at is None

    assert (await client.get("/v1/jobs", headers=_key_headers(raw))).status_code == 200

    async with sm() as db:
        after = await db.scalar(select(ApiKey).where(ApiKey.prefix == raw[: len("cc_live_") + 8]))
        assert after.last_used_at is not None, "using a key must stamp last_used_at"


@pytest.mark.asyncio
async def test_api_key_cannot_be_used_for_an_unverified_spend_path(client):
    """A key inherits the owner's verification state, so it cannot be used to
    sidestep the email-verification gate on spending."""
    from tests.helpers import register_user

    reg = await register_user(client, "apikey-unverified@example.com")
    # Deliberately NOT verifying the email.
    login_resp = await client.post(
        "/auth/login",
        json={"email": "apikey-unverified@example.com", "password": "Sup3rStrongPw!"},
    )
    assert login_resp.status_code == 200, login_resp.text
    token = login_resp.json()["access_token"]

    project = await client.post(
        "/projects", json={"name": "unverified"}, headers=auth_headers(token)
    )
    if project.status_code != 201:
        pytest.skip("project creation itself requires verification here")
    raw = await _issue_key(client, token, project.json()["id"])

    resp = await client.post(
        "/v1/billing/credits/purchase",
        json={"amount": "10.00"},
        headers=_key_headers(raw),
    )
    assert resp.status_code == 403
    assert "email_not_verified" in resp.text
