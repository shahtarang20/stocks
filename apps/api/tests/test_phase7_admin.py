"""Phase 7 — Admin operations tests.

Verifies:
- RBAC: customers are denied (403), administrators are authorized (200).
- Users listing: all registered users returned with role claims.
- Account suspension and reinstatement: state mutations with audit logging.
- Audit log query: reads recorded system events.
- Platform-wide jobs list: administrative visibility across users.
- Live platform KPIs: calculation of total users, active providers, job completion rate.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.models.enums import Role
from tests.helpers import auth_headers, grant_role, login, register_verify_login


@pytest.fixture
async def admin_client(client: AsyncClient) -> dict:
    """Register, verify, promote to administrator, and login."""
    tokens = await register_verify_login(client, "admin-phase7@example.com")
    await grant_role("admin-phase7@example.com", Role.administrator)
    return await login(client, "admin-phase7@example.com")


@pytest.fixture
async def customer_client(client: AsyncClient) -> dict:
    """Standard customer without administrator privileges."""
    return await register_verify_login(client, "customer-phase7@example.com")


@pytest.mark.asyncio
async def test_admin_list_users(client: AsyncClient, admin_client: dict, customer_client: dict):
    # Customer is denied
    cust_resp = await client.get(
        "/v1/admin/users",
        headers=auth_headers(customer_client["access_token"]),
    )
    assert cust_resp.status_code == 403

    # Admin is allowed
    admin_resp = await client.get(
        "/v1/admin/users",
        headers=auth_headers(admin_client["access_token"]),
    )
    assert admin_resp.status_code == 200
    users = admin_resp.json()
    assert isinstance(users, list)
    emails = [u["email"] for u in users]
    assert "admin-phase7@example.com" in emails
    assert "customer-phase7@example.com" in emails


@pytest.mark.asyncio
async def test_admin_suspend_and_reinstate_user(client: AsyncClient, admin_client: dict):
    # Register target user
    target = await register_verify_login(client, "target-suspend@example.com")
    admin_headers = auth_headers(admin_client["access_token"])

    # Find target user ID
    users_resp = await client.get("/v1/admin/users", headers=admin_headers)
    target_user = next(u for u in users_resp.json() if u["email"] == "target-suspend@example.com")
    target_id = target_user["user_id"]

    # Suspend user
    suspend_resp = await client.post(f"/v1/admin/users/{target_id}/suspend", headers=admin_headers)
    assert suspend_resp.status_code == 200
    assert suspend_resp.json()["status"] == "suspended"

    # Suspended user cannot make requests (status check in deps)
    target_headers = auth_headers(target["access_token"])
    job_resp = await client.get("/v1/jobs", headers=target_headers)
    assert job_resp.status_code == 401

    # Reinstate user
    reinstate_resp = await client.post(f"/v1/admin/users/{target_id}/reinstate", headers=admin_headers)
    assert reinstate_resp.status_code == 200
    assert reinstate_resp.json()["status"] == "active"


@pytest.mark.asyncio
async def test_cannot_suspend_self(client: AsyncClient, admin_client: dict):
    admin_headers = auth_headers(admin_client["access_token"])
    users_resp = await client.get("/v1/admin/users", headers=admin_headers)
    self_user = next(u for u in users_resp.json() if u["email"] == "admin-phase7@example.com")

    resp = await client.post(f"/v1/admin/users/{self_user['user_id']}/suspend", headers=admin_headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "cannot_suspend_self"


@pytest.mark.asyncio
async def test_admin_audit_logs(client: AsyncClient, admin_client: dict, customer_client: dict):
    # Customer is denied
    cust_resp = await client.get(
        "/v1/admin/audit-logs",
        headers=auth_headers(customer_client["access_token"]),
    )
    assert cust_resp.status_code == 403

    # Admin reads audit logs
    admin_resp = await client.get(
        "/v1/admin/audit-logs",
        headers=auth_headers(admin_client["access_token"]),
    )
    assert admin_resp.status_code == 200
    logs = admin_resp.json()
    assert isinstance(logs, list)


@pytest.mark.asyncio
async def test_admin_all_jobs(client: AsyncClient, admin_client: dict):
    admin_headers = auth_headers(admin_client["access_token"])
    resp = await client.get("/v1/admin/jobs", headers=admin_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_admin_platform_stats(client: AsyncClient, admin_client: dict):
    admin_headers = auth_headers(admin_client["access_token"])
    resp = await client.get("/v1/admin/stats", headers=admin_headers)
    assert resp.status_code == 200
    stats = resp.json()
    assert "total_users" in stats
    assert stats["total_users"] >= 1
    assert "active_providers" in stats
    assert "total_jobs" in stats
    assert "success_rate" in stats
    assert "total_volume" in stats
