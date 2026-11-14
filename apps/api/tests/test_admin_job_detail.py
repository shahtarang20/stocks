"""An administrator can open ANY job (state, host, cost, retries, timeline), but never sees its content."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.enums import Role
from app.models.system import AuditLog
from tests.helpers import auth_headers, grant_role, login, register_verify_login
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import (  # noqa: F401  (_artifact_tmp is an autouse fixture)
    _artifact_tmp,
    _customer,
    claim,
    seed_echo_model,
    submit_job,
)

pytestmark = pytest.mark.asyncio

SECRET_PROMPT = "my-very-secret-prompt-text-12345"


async def _admin(client) -> dict:
    await register_verify_login(client, "admin-jobview@example.com")
    await grant_role("admin-jobview@example.com", Role.administrator)
    return await login(client, "admin-jobview@example.com")


async def _job(client, tag: str):
    await seed_echo_model()
    machine = await create_listable_machine(client, f"p-{tag}@x.com", hostname=f"host-{tag}")
    customer = await _customer(client, f"c-{tag}@x.com")
    sub = await submit_job(client, customer, machine_id=machine["machine_id"], input={"prompt": SECRET_PROMPT})
    assert sub.status_code == 201, sub.text
    return uuid.UUID(sub.json()["job_id"]), customer, machine


async def test_admin_can_open_another_customers_job_with_its_story(client):
    job_id, customer, machine = await _job(client, "av1")
    assert (await claim(client, machine["machine_id"], machine["secret"])).status_code == 200
    admin = await _admin(client)

    # The customer's own endpoint refuses the admin (not their job); the admin endpoint answers.
    assert (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(admin["access_token"]))).status_code == 404
    res = await client.get(f"/v1/admin/jobs/{job_id}", headers=auth_headers(admin["access_token"]))
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["job_id"] == str(job_id)
    assert body["customer_email"] == "c-av1@x.com"
    assert body["status"] == "running"
    assert body["machine_id"] == machine["machine_id"]
    assert body["machine_hostname"] == "host-av1"
    assert body["attempts_made"] == 0
    # The timeline and the attempt are there.
    assert [e["to_state"] for e in body["events"]][-1] == "running"
    assert body["events"] == sorted(body["events"], key=lambda e: e["seq"])
    assert len(body["attempts"]) == 1 and body["attempts"][0]["status"] == "running"
    assert body["attempts"][0]["machine_id"] == machine["machine_id"]


async def test_admin_job_view_never_contains_the_customers_content(client):
    job_id, _customer_token, _machine = await _job(client, "av2")
    admin = await _admin(client)
    res = await client.get(f"/v1/admin/jobs/{job_id}", headers=auth_headers(admin["access_token"]))
    assert res.status_code == 200
    body = res.json()
    # No content fields at all, and the prompt text appears nowhere in the response.
    for field in ("input", "output", "encrypted_input", "encrypted_output", "data_policy", "match_explanation"):
        assert field not in body
    assert SECRET_PROMPT not in res.text


async def test_customers_cannot_use_the_admin_job_view_even_for_their_own_job(client):
    job_id, customer, _machine = await _job(client, "av3")
    res = await client.get(f"/v1/admin/jobs/{job_id}", headers=auth_headers(customer))
    assert res.status_code == 403


async def test_signed_out_visitors_and_unknown_jobs(client):
    job_id, _customer_token, _machine = await _job(client, "av4")
    assert (await client.get(f"/v1/admin/jobs/{job_id}")).status_code in (401, 403)
    admin = await _admin(client)
    missing = await client.get(f"/v1/admin/jobs/{uuid.uuid4()}", headers=auth_headers(admin["access_token"]))
    assert missing.status_code == 404
    assert (await client.get("/v1/admin/jobs/not-a-uuid", headers=auth_headers(admin["access_token"]))).status_code == 422


async def test_every_admin_view_is_written_to_the_audit_log(client):
    job_id, _customer_token, _machine = await _job(client, "av5")
    admin = await _admin(client)
    await client.get(f"/v1/admin/jobs/{job_id}", headers=auth_headers(admin["access_token"]))

    async with get_sessionmaker()() as s:
        rows = (
            await s.execute(
                select(AuditLog).where(AuditLog.action == "admin.job.view", AuditLog.resource_id == str(job_id))
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].resource_type == "job" and rows[0].outcome == "success" and rows[0].actor_user_id is not None


async def test_the_existing_admin_job_list_still_works(client):
    job_id, _customer_token, _machine = await _job(client, "av6")
    admin = await _admin(client)
    res = await client.get("/v1/admin/jobs", headers=auth_headers(admin["access_token"]))
    assert res.status_code == 200
    assert str(job_id) in [j["job_id"] for j in res.json()]
