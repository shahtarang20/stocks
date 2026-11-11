"""A job that keeps heartbeating must still stop at max_runtime_seconds (+ the staging grace)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.jobs import Job
from app.services.leases import recover_expired_leases
from tests.helpers import auth_headers
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import (  # noqa: F401  (_artifact_tmp is an autouse fixture)
    _artifact_tmp,
    _customer,
    claim,
    seed_echo_model,
    submit_job,
)

pytestmark = pytest.mark.asyncio


async def _running_job(client, tag: str):
    await seed_echo_model()
    m = await create_listable_machine(client, f"p-{tag}@x.com", hostname=tag)
    cust = await _customer(client, f"c-{tag}@x.com")
    sub = await submit_job(client, cust, machine_id=m["machine_id"])
    assert sub.status_code == 201, sub.text
    job_id = uuid.UUID(sub.json()["job_id"])
    assert (await claim(client, m["machine_id"], m["secret"])).status_code == 200
    # A healthy heartbeat: the lease is far in the future.
    async with get_sessionmaker()() as s:
        job = await s.get(Job, job_id)
        job.lease_expires_at = utcnow() + dt.timedelta(days=30)
        assert job.status == "running"
        limit = int(job.max_runtime_seconds)
        await s.commit()
    return job_id, cust, limit


async def _sweep(at: dt.datetime) -> list[dict]:
    async with get_sessionmaker()() as s:
        out = await recover_expired_leases(s, at)
        await s.commit()
    return out


async def test_heartbeating_job_is_left_alone_within_its_runtime_and_grace(client):
    job_id, _cust, limit = await _running_job(client, "mr-ok")
    inside = utcnow() + dt.timedelta(seconds=limit + settings.JOB_RUNTIME_GRACE_SECONDS - 60)
    assert [r for r in await _sweep(inside) if r["job_id"] == str(job_id)] == []
    async with get_sessionmaker()() as s:
        assert (await s.get(Job, job_id)).status == "running"


async def test_heartbeating_job_past_max_runtime_plus_grace_is_timed_out_and_not_charged(client):
    job_id, cust, limit = await _running_job(client, "mr-over")
    held = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()["reserved"]
    assert held != "0.00"

    beyond = utcnow() + dt.timedelta(seconds=limit + settings.JOB_RUNTIME_GRACE_SECONDS + 60)
    record = next(r for r in await _sweep(beyond) if r["job_id"] == str(job_id))
    assert record["action"] == "timed_out"

    async with get_sessionmaker()() as s:
        job = await s.get(Job, job_id)
        assert job.status == "timed_out"
        assert "max runtime exceeded" in (job.error_message or "")
    # The hold is returned in full: an overrun job is never charged.
    assert (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()["reserved"] == "0.00"
