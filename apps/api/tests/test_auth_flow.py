"""Happy path: register -> verify-email -> login."""

from __future__ import annotations

import pytest

from tests.helpers import login, register_user, verify_email


@pytest.mark.asyncio
async def test_register_verify_login_happy_path(client):
    reg = await register_user(client, "alice@example.com")
    assert reg["email"] == "alice@example.com"
    assert reg["email_verification_token"]  # dev token returned

    await verify_email(client, reg["email_verification_token"])

    tokens = await login(client, "alice@example.com")
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] > 0


@pytest.mark.asyncio
async def test_email_is_case_insensitive(client):
    await register_user(client, "Bob@Example.com")
    # Duplicate with different casing is rejected.
    resp = await client.post(
        "/auth/register", json={"email": "bob@example.com", "password": "Sup3rStrongPw!"}
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_verify_email_token_single_use(client):
    reg = await register_user(client, "carol@example.com")
    token = reg["email_verification_token"]
    await verify_email(client, token)
    # Reusing the token fails.
    resp = await client.post("/auth/verify-email", json={"token": token})
    assert resp.status_code == 400
