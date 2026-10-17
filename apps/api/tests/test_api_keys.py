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
