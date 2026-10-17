"""Password reset: single-use token + all sessions revoked after reset."""

from __future__ import annotations

import pytest

from tests.helpers import login, register_verify_login


@pytest.mark.asyncio
async def test_password_reset_single_use_and_revokes_sessions(client):
    tokens = await register_verify_login(client, "harry@example.com")
    old_refresh = tokens["refresh_token"]

    # Request a reset token.
    req = await client.post(
        "/auth/password-reset/request", json={"email": "harry@example.com"}
    )
    assert req.status_code == 200, req.text
    reset_token = req.json()["password_reset_token"]
    assert reset_token

    # Confirm with a new password.
    new_password = "Even-Str0nger-Pw!"
    confirm = await client.post(
        "/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": new_password},
    )
    assert confirm.status_code == 200, confirm.text

    # Old session is revoked -> old refresh token no longer works.
    refresh_resp = await client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert refresh_resp.status_code == 401

    # The token is single-use -> second confirm fails.
    reuse = await client.post(
        "/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "Another-Pw-123!"},
    )
    assert reuse.status_code == 400

    # New password works; old password does not.
    assert (await login(client, "harry@example.com", new_password))["access_token"]
    bad = await client.post(
        "/auth/login", json={"email": "harry@example.com", "password": "Sup3rStrongPw!"}
    )
    assert bad.status_code == 401


@pytest.mark.asyncio
async def test_password_reset_unknown_email_is_generic(client):
    resp = await client.post(
        "/auth/password-reset/request", json={"email": "nobody@example.com"}
    )
    # Generic response, no enumeration, no token issued.
    assert resp.status_code == 200
    assert resp.json()["password_reset_token"] is None
