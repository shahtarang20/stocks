"""GET /auth/csrf lets a cross-origin SPA recover its CSRF token after a reload."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.test_cookie_auth_csrf import _seed_user


@pytest.mark.asyncio
async def test_csrf_endpoint_returns_cookie_value_and_unblocks_mutations(client: AsyncClient, db_engine):
    user = await _seed_user(db_engine, "csrf_endpoint@test.com", "Password123!")
    login = await client.post("/auth/login", json={"email": user.email, "password": "Password123!"})
    assert login.status_code == 200
    cookies = login.cookies  # a reloaded page holds these cookies but no in-memory token

    res = await client.get("/auth/csrf", cookies=cookies)
    assert res.status_code == 200
    assert res.json()["csrf_token"] == login.json()["csrf_token"]
    assert res.headers["cache-control"] == "no-store"

    # Using it as the header satisfies the double-submit check on a cookie-authenticated mutation.
    ok = await client.patch(
        "/auth/me", json={"display_name": "After reload"}, cookies=cookies,
        headers={"X-CSRF-Token": res.json()["csrf_token"]},
    )
    assert ok.status_code == 200
    # ...and without it the same request is still rejected.
    denied = await client.patch("/auth/me", json={"display_name": "x"}, cookies=cookies)
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_csrf_endpoint_requires_a_session(client: AsyncClient):
    assert (await client.get("/auth/csrf")).status_code == 401
    # a lone csrf cookie with no session cookie is not enough
    assert (await client.get("/auth/csrf", cookies={"csrf_token": "abc"})).status_code == 401
