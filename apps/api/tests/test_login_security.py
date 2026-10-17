"""Wrong-password generic error + rate limiting."""

from __future__ import annotations

import pytest

from app.core.config import settings
from tests.helpers import register_user


@pytest.mark.asyncio
async def test_wrong_password_generic_error(client):
    await register_user(client, "dave@example.com")
    resp = await client.post(
        "/auth/login", json={"email": "dave@example.com", "password": "wrong-password"}
    )
    assert resp.status_code == 401
    # Generic message: no hint whether the user exists.
    assert resp.json()["detail"] == "Invalid credentials"


@pytest.mark.asyncio
async def test_login_rate_limit_returns_429(client):
    await register_user(client, "erin@example.com")
    limit = settings.LOGIN_RATE_LIMIT
    saw_429 = False
    # Attempt more than the limit; once exceeded we must get a 429.
    for _ in range(limit + 2):
        resp = await client.post(
            "/auth/login", json={"email": "erin@example.com", "password": "nope"}
        )
        if resp.status_code == 429:
            saw_429 = True
            break
    assert saw_429, "rate limiter never triggered a 429"


@pytest.mark.asyncio
async def test_login_unknown_user_generic_error(client):
    resp = await client.post(
        "/auth/login", json={"email": "ghost@example.com", "password": "whatever123"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"
