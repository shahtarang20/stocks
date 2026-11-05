"""A multi-node member whose rendezvous cannot be built is failed AND that outcome is committed.

The claim route used to flush the failure (job FAILED, hold released, reservation freed) and then raise an
HTTPException; ``get_db`` rolls the session back on any exception, so the failure was undone and the job stayed
RUNNING on the machine until its lease expired."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.billing import LedgerTransaction
from app.models.jobs import Job
from app.services import multi_node
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import (  # noqa: F401  (autouse fixture: object store + size cap)
    _artifact_tmp,
    claim,
    seed_echo_model,
    submit_job,
)
from tests.helpers import register_verify_login

pytestmark = pytest.mark.asyncio

PLAN = {"kit": {"slug": "k", "version": "1"}, "inputs": [], "assets": [], "outputs": [], "egress": [], "checkpoint": None}


async def test_failed_rendezvous_claim_is_committed_not_rolled_back(client, monkeypatch):
    await seed_echo_model()
    machine = await create_listable_machine(client, "rv-prov@x.com", hostname="rv")
    token = (await register_verify_login(client, "rv-cust@x.com"))["access_token"]
    sub = await submit_job(client, token)
    assert sub.status_code == 201, sub.text
    job_id = uuid.UUID(sub.json()["job_id"])
    async with get_sessionmaker()() as db:  # turn it into a (multi-node) Kit job
        job = await db.get(Job, job_id)
        job.kit_id, job.kit_version = "k", "1"
        job.spec_v2 = {"spec": {}, "effective_resources": {"gpu_count": 1}, "kit_image": "r/x@sha256:" + "a" * 64}
        job.data_policy = {"v2_plan": PLAN}
        await db.commit()

    async def no_rendezvous(db, job):
        return True, None  # a member of a node group, but the master address is unavailable

    monkeypatch.setattr(multi_node, "rendezvous_for_job", no_rendezvous)
    r = await claim(client, machine["machine_id"], machine["secret"])
    assert r.status_code == 409 and r.json()["detail"] == "rendezvous_unavailable", r.text

    async with get_sessionmaker()() as db:
        job = await db.get(Job, job_id)
        assert job.status == "failed" and job.error_code == "rendezvous_unavailable"  # not left RUNNING
        keys = (await db.execute(select(LedgerTransaction.idempotency_key).where(
            LedgerTransaction.idempotency_key == f"release_hold:{job_id}"))).scalars().all()
        assert keys == [f"release_hold:{job_id}"]  # the customer's hold came back
