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
async def test_admin_audit_logs_serialize_a_real_entry(
    client: AsyncClient, admin_client: dict
):
    """The listing must survive a NON-EMPTY audit table.

    Asserting only 200-and-a-list never runs the per-row serializer, which is
    precisely how the /v1/admin/jobs AttributeError survived a passing test.
    So: cause a real audited action first, then read it back and check fields.
    """
    admin_headers = auth_headers(admin_client["access_token"])
    target = await register_verify_login(client, "audited-target@example.com")
    assert target is not None

    users = (await client.get("/v1/admin/users", headers=admin_headers)).json()
    target_id = next(
        u["user_id"] for u in users if u["email"] == "audited-target@example.com"
    )
    assert (
        await client.post(f"/v1/admin/users/{target_id}/suspend", headers=admin_headers)
    ).status_code == 200

    resp = await client.get("/v1/admin/audit-logs", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    logs = resp.json()
    assert len(logs) >= 1, "the suspend should have written an audit entry"

    entry = logs[0]
    for field in ("id", "action", "outcome", "created_at"):
        assert field in entry, f"audit entry is missing {field}: {entry}"
    assert any("suspend" in (log.get("action") or "") for log in logs), (
        f"no suspend action in the audit log: {[l.get('action') for l in logs][:5]}"
    )


@pytest.mark.asyncio
async def test_admin_all_jobs(client: AsyncClient, admin_client: dict):
    admin_headers = auth_headers(admin_client["access_token"])
    resp = await client.get("/v1/admin/jobs", headers=admin_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_admin_all_jobs_serializes_a_real_job(
    client: AsyncClient, admin_client: dict, customer_client: dict
):
    """The listing must survive a NON-EMPTY job table.

    Asserting only 200-and-a-list passes against a broken endpoint, because an
    empty result never runs the per-row serialization. This endpoint really did
    raise AttributeError on every row until the field name was corrected, and an
    empty-list test could not see it.
    """
    import decimal
    import uuid as _uuid

    from app.db.session import get_sessionmaker
    from app.models.jobs import Job
    from app.models.user import User
    from sqlalchemy import select

    sm = get_sessionmaker()
    async with sm() as db:
        customer = await db.scalar(
            select(User).where(User.email == "customer-phase7@example.com")
        )
        assert customer is not None
        db.add(
            Job(
                id=_uuid.uuid4(),
                customer_id=customer.id,
                workload_type="text-generation",
                model="echo-sim-v1",
                input={"prompt": "hi"},
                status="completed",
                execution_mode="simulated",
                gpu_count=1,
                estimated_minutes=1,
                max_runtime_seconds=600,
                retry_policy={},
                attempts_made=1,
                priority=0,
                final_cost=decimal.Decimal("1.23"),
            )
        )
        await db.commit()

    resp = await client.get("/v1/admin/jobs", headers=admin_headers_of(admin_client))
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) >= 1
    row = next(r for r in rows if r["model"] == "echo-sim-v1")
    # Money is a 2dp string, never a float.
    assert row["final_cost"] == "1.23"
    assert row["execution_mode"] == "simulated"


def admin_headers_of(admin_client: dict) -> dict:
    return auth_headers(admin_client["access_token"])


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


@pytest.mark.asyncio
async def test_admin_lists_all_machines_without_being_a_provider(
    client: AsyncClient, admin_client: dict, customer_client: dict
):
    """/provider/machines is scoped to the caller's own provider profile, so the
    admin telemetry picker 403'd for every administrator who is not a provider.
    /v1/admin/machines is the network-wide list and is administrator-only."""
    from tests.test_provider_phase2 import become_provider, enroll_machine, mint_token

    prov = await become_provider(client, "machine-owner-phase7@example.com")
    token = await mint_token(client, prov)
    enrolled = await enroll_machine(client, token["enrollment_token"], hostname="admin-view-1")

    admin_headers = auth_headers(admin_client["access_token"])
    assert (await client.get("/provider/machines", headers=admin_headers)).status_code == 403

    resp = await client.get("/v1/admin/machines", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert enrolled["machine_id"] in [m["machine_id"] for m in resp.json()]

    denied = await client.get(
        "/v1/admin/machines", headers=auth_headers(customer_client["access_token"])
    )
    assert denied.status_code == 403
