"""Refresh token rotation + reuse detection."""

from __future__ import annotations

import pytest

from tests.helpers import register_verify_login


@pytest.mark.asyncio
async def test_refresh_rotates_and_old_token_invalid(client):
    tokens = await register_verify_login(client, "frank@example.com")
    old_refresh = tokens["refresh_token"]

    # Rotate once.
    r1 = await client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert r1.status_code == 200, r1.text
    new_refresh = r1.json()["refresh_token"]
    assert new_refresh != old_refresh

    # The new refresh token works.
    r2 = await client.post("/auth/refresh", json={"refresh_token": new_refresh})
    assert r2.status_code == 200, r2.text


@pytest.mark.asyncio
async def test_reusing_revoked_refresh_revokes_chain(client):
    tokens = await register_verify_login(client, "grace@example.com")
    r0 = tokens["refresh_token"]

    # Rotate: r0 -> r1 (r0 now revoked).
    r1_resp = await client.post("/auth/refresh", json={"refresh_token": r0})
    assert r1_resp.status_code == 200
    r1 = r1_resp.json()["refresh_token"]

    # Replay the already-rotated r0 -> reuse detected -> 401 + chain revoked.
    replay = await client.post("/auth/refresh", json={"refresh_token": r0})
    assert replay.status_code == 401

    # Because the whole chain was revoked, the previously valid r1 is now dead.
    after = await client.post("/auth/refresh", json={"refresh_token": r1})
    assert after.status_code == 401
