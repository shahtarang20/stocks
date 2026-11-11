"""When the HOST ends an attempt (lost lease, thermal shutdown, interruption) the platform re-dispatches the job on another
machine for free, on top of the customer's own retry_policy. The customer is never charged for the host's problem."""

from __future__ import annotations

import datetime as dt
import types
import uuid

import pytest

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.jobs import Job
from app.services import leases
from tests.helpers import auth_headers
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import (  # noqa: F401  (_artifact_tmp is an autouse fixture)
    _artifact_tmp,
    _customer,
    claim,
    post_result,
    seed_echo_model,
    submit_job,
)

pytestmark = pytest.mark.asyncio


async def _expire_lease(job_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as s:
        job = await s.get(Job, job_id)
        job.lease_expires_at = utcnow() - dt.timedelta(seconds=10)
        await s.commit()


async def _recover() -> list[dict]:
    async with get_sessionmaker()() as s:
        out = await leases.recover_expired_leases(s, utcnow())
        await s.commit()
    return out


async def _job(job_id: uuid.UUID) -> Job:
    async with get_sessionmaker()() as s:
        return await s.get(Job, job_id)


async def _reserved(client, token) -> str:
    return (await client.get("/v1/billing/account", headers=auth_headers(token))).json()["reserved"]


async def test_lost_lease_gets_one_free_retry_even_when_the_customer_set_no_retries(client):
    await seed_echo_model()
    a = await create_listable_machine(client, "p-hf1a@x.com", hostname="hf1a")
    b = await create_listable_machine(client, "p-hf1b@x.com", hostname="hf1b")
    cust = await _customer(client, "c-hf1@x.com")
    sub = await submit_job(client, cust, machine_id=a["machine_id"])  # default retry_policy: max_retries 0
    job_id = uuid.UUID(sub.json()["job_id"])
    held = await _reserved(client, cust)

    assert (await claim(client, a["machine_id"], a["secret"])).status_code == 200
    await _expire_lease(job_id)
    rec = next(r for r in await _recover() if r["job_id"] == str(job_id))
    assert rec["action"] == "requeued" and rec["machine_id"] == b["machine_id"]
    assert await _reserved(client, cust) == held  # hold kept, never re-posted: at most one charge

    # The second loss spends the free budget: now it times out and the whole hold comes back.
    assert (await claim(client, b["machine_id"], b["secret"])).status_code == 200
    await _expire_lease(job_id)
    rec2 = next(r for r in await _recover() if r["job_id"] == str(job_id))
    assert rec2["action"] == "timed_out"
    assert await _reserved(client, cust) == "0.00"


async def test_free_retries_can_be_switched_off(client, monkeypatch):
    monkeypatch.setattr(settings, "HOST_FAILURE_FREE_RETRIES", 0)
    await seed_echo_model()
    a = await create_listable_machine(client, "p-hf2a@x.com", hostname="hf2a")
    await create_listable_machine(client, "p-hf2b@x.com", hostname="hf2b")
    cust = await _customer(client, "c-hf2@x.com")
    job_id = uuid.UUID((await submit_job(client, cust, machine_id=a["machine_id"])).json()["job_id"])
    assert (await claim(client, a["machine_id"], a["secret"])).status_code == 200
    await _expire_lease(job_id)
    assert next(r for r in await _recover() if r["job_id"] == str(job_id))["action"] == "timed_out"


async def test_thermal_shutdown_is_retried_on_another_machine_not_failed(client):
    await seed_echo_model()
    a = await create_listable_machine(client, "p-hf3a@x.com", hostname="hf3a")
    b = await create_listable_machine(client, "p-hf3b@x.com", hostname="hf3b")
    cust = await _customer(client, "c-hf3@x.com")
    job_id = uuid.UUID((await submit_job(client, cust, machine_id=a["machine_id"])).json()["job_id"])
    held = await _reserved(client, cust)
    assert (await claim(client, a["machine_id"], a["secret"])).status_code == 200

    res = await post_result(
        client, a["machine_id"], a["secret"], job_id,
        {"status": "failed", "execution_mode": "simulated", "error": {"code": "thermal_shutdown", "message": "95 C"}},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] != "failed"
    job = await _job(job_id)
    assert job.status in ("reserved", "queued") and job.machine_id == uuid.UUID(b["machine_id"])
    assert job.attempts_made == 1 and job.error_code is None
    assert await _reserved(client, cust) == held

    # A second thermal shutdown spends the free budget: now it really fails, and the hold is returned.
    assert (await claim(client, b["machine_id"], b["secret"])).status_code == 200
    res2 = await post_result(
        client, b["machine_id"], b["secret"], job_id,
        {"status": "failed", "execution_mode": "simulated", "error": {"code": "thermal_shutdown", "message": "95 C"}},
    )
    assert res2.json()["status"] == "failed"
    assert (await _job(job_id)).error_code == "thermal_shutdown"
    assert await _reserved(client, cust) == "0.00"


async def test_a_failure_of_the_customers_own_code_is_not_retried_for_free(client):
    await seed_echo_model()
    a = await create_listable_machine(client, "p-hf4a@x.com", hostname="hf4a")
    await create_listable_machine(client, "p-hf4b@x.com", hostname="hf4b")
    cust = await _customer(client, "c-hf4@x.com")
    job_id = uuid.UUID((await submit_job(client, cust, machine_id=a["machine_id"])).json()["job_id"])
    assert (await claim(client, a["machine_id"], a["secret"])).status_code == 200
    res = await post_result(
        client, a["machine_id"], a["secret"], job_id,
        {"status": "failed", "execution_mode": "simulated", "error": {"code": "oom", "message": "out of memory"}},
    )
    assert res.json()["status"] == "failed"
    assert (await _job(job_id)).attempts_made == 0


def test_platform_audit_jobs_and_service_replicas_never_get_free_retries():
    plain = types.SimpleNamespace(is_challenge=False, shadow_of_job_id=None, data_policy={})
    challenge = types.SimpleNamespace(is_challenge=True, shadow_of_job_id=None, data_policy={})
    shadow = types.SimpleNamespace(is_challenge=False, shadow_of_job_id=uuid.uuid4(), data_policy={})
    replica = types.SimpleNamespace(is_challenge=False, shadow_of_job_id=None, data_policy={"v2_plan": {"service": {"port": 8000}}})
    assert leases.host_failure_retry_budget(plain) == settings.HOST_FAILURE_FREE_RETRIES
    assert leases.host_failure_retry_budget(challenge) == 0
    assert leases.host_failure_retry_budget(shadow) == 0
    assert leases.host_failure_retry_budget(replica) == 0
