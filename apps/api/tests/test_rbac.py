"""RBAC: customer denied on admin-only route; admin allowed."""

from __future__ import annotations

import pytest

from app.models.enums import Role
from tests.helpers import auth_headers, grant_role, login, register_verify_login


@pytest.mark.asyncio
async def test_customer_denied_on_admin_route(client):
    tokens = await register_verify_login(client, "heidi@example.com")
    resp = await client.get("/admin/ping", headers=auth_headers(tokens["access_token"]))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_allowed_on_admin_route(client):
    tokens = await register_verify_login(client, "ivan@example.com")
    await grant_role("ivan@example.com", Role.administrator)
    # Re-login so the fresh access token carries the administrator role claim.
    tokens = await login(client, "ivan@example.com")
    resp = await client.get("/admin/ping", headers=auth_headers(tokens["access_token"]))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_admin_route_requires_auth(client):
    resp = await client.get("/admin/ping")
    assert resp.status_code == 401
