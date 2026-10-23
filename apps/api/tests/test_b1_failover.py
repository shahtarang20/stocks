"""Phase B1 — failover & retry with idempotent billing.

Proves the four guarantees that make a dead provider survivable:

a) a job still COMPLETES after its first provider crashes, and the customer is
   charged EXACTLY ONCE (one hold, one capture);
b) the machine that just failed is NOT re-selected while an alternative exists;
c) once retries are exhausted the job is terminal and the ENTIRE hold is
   returned (a failed job is never charged);
d) no capacity leaks — every reservation taken by a dead attempt is released.

Plus: a cancelled job is never retried, and the failure is attributed to the
provider in the append-only reputation log.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select

from app.db.session import get_sessionmaker
from app.db.base import utcnow
from app.models.billing import LedgerTransaction
from app.models.jobs import Job, JobAttempt
from app.models.marketplace import ResourceReservation
from app.models.reputation import ReputationEvent
from tests.helpers import auth_headers
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import (
    _artifact_tmp,  # noqa: F401  (autouse fixture: temp artifact store)
    _customer,
    claim,
    post_result,
    seed_echo_model,
    submit_job,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _expire_lease(job_id: uuid.UUID) -> None:
    """Simulate a provider crash: the lease lapses with no heartbeat."""
    sm = get_sessionmaker()
    async with sm() as s:
        job = await s.get(Job, job_id)
        job.lease_expires_at = utcnow() - dt.timedelta(seconds=10)
        await s.commit()


async def _recover() -> list[dict]:
    from app.services.leases import recover_expired_leases

    sm = get_sessionmaker()
    async with sm() as s:
        recovered = await recover_expired_leases(s, utcnow())
        await s.commit()
    return recovered


async def _active_slots(machine_id: str) -> int:
    sm = get_sessionmaker()
    async with sm() as s:
        rows = (
            await s.execute(
                select(ResourceReservation).where(
                    ResourceReservation.machine_id == uuid.UUID(machine_id),
                    ResourceReservation.status == "active",
                )
            )
        ).scalars().all()
    return sum(r.slots for r in rows)


async def _txn_count(job_id: uuid.UUID, kind: str) -> int:
    sm = get_sessionmaker()
    async with sm() as s:
        return int(
            (
                await s.execute(
                    select(func.count(LedgerTransaction.id)).where(
                        LedgerTransaction.kind == kind,
                        LedgerTransaction.reference_type == "job",
                        LedgerTransaction.reference_id == str(job_id),
                    )
                )
            ).scalar()
            or 0
        )


async def _job_machine(job_id: uuid.UUID) -> uuid.UUID | None:
    sm = get_sessionmaker()
    async with sm() as s:
        job = await s.get(Job, job_id)
        return job.machine_id


async def _acct(client, token) -> dict:
    return (await client.get("/v1/billing/account", headers=auth_headers(token))).json()


# ===========================================================================
# (a) + (b) failover onto a DIFFERENT machine, charged exactly once
# ===========================================================================
async def test_failover_to_other_machine_and_exactly_one_charge(client):
    await seed_echo_model()
    a = await create_listable_machine(client, "p-b1a@x.com", hostname="b1a")
    b = await create_listable_machine(client, "p-b1b@x.com", hostname="b1b")
    cust = await _customer(client, "c-b1-ok@x.com")

    # Target machine A so the first (doomed) attempt is deterministic.
    # 120 estimated minutes -> a 3.30 authorization; the job then meters one
    # real hour (1.65), so capture < hold and the remainder must come back.
    sub = await submit_job(
        client, cust, machine_id=a["machine_id"], estimated_minutes=120,
        retry_policy={"max_retries": 1},
    )
    assert sub.status_code == 201, sub.text
    job_id = uuid.UUID(sub.json()["job_id"])
    assert sub.json()["machine_id"] == a["machine_id"]

    acct_held = await _acct(client, cust)
    assert acct_held["reserved"] == "3.30"
    held_before = acct_held["reserved"]

    # A claims, then dies (lease lapses).
    assert (await claim(client, a["machine_id"], a["secret"])).status_code == 200
    await _expire_lease(job_id)

    recovered = await _recover()
    record = next(r for r in recovered if r["job_id"] == str(job_id))
    # (b) failed machine excluded while an alternative exists.
    assert record["action"] == "requeued"
    assert record["rescheduled"] is True
    assert record["failed_over"] is True
    assert record["machine_id"] == b["machine_id"], record
    assert await _job_machine(job_id) == uuid.UUID(b["machine_id"])

    # (d) A's capacity was handed back; exactly one live reservation, on B.
    assert await _active_slots(a["machine_id"]) == 0
    assert await _active_slots(b["machine_id"]) == 1

    # The hold was NOT re-posted by the reschedule.
    assert await _txn_count(job_id, "hold") == 1
    assert (await _acct(client, cust))["reserved"] == held_before

    # A cannot steal the job back; B runs it to completion.
    assert (await claim(client, a["machine_id"], a["secret"])).status_code == 204
    claimed = await claim(client, b["machine_id"], b["secret"])
    assert claimed.status_code == 200, claimed.text
    result = await post_result(
        client, b["machine_id"], b["secret"], str(job_id),
        {
            "status": "success",
            "execution_mode": "simulated",
            "output": {"text": "ok", "simulated": True},
            "metrics": {"duration_seconds": 3600},
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed"

    # (a) EXACTLY ONE charge: one hold, one capture, one invoice.
    assert await _txn_count(job_id, "hold") == 1
    assert await _txn_count(job_id, "capture") == 1
    invoices = (await client.get("/v1/invoices", headers=auth_headers(cust))).json()
    assert len([i for i in invoices if i["job_id"] == str(job_id)]) == 1

    # Balance moved by the metered cost once (1.50/h * 1h + 10% fee = 1.65).
    acct_end = await _acct(client, cust)
    assert acct_end["reserved"] == "0.00"
    assert acct_end["balance"] == "998.35"
    assert acct_end["available"] == "998.35"

    # (d) no capacity leak anywhere once the job is terminal.
    assert await _active_slots(a["machine_id"]) == 0
    assert await _active_slots(b["machine_id"]) == 0


# ===========================================================================
# (b) negative control: with no alternative the SAME machine is retried
# ===========================================================================
async def test_single_machine_still_retries_on_the_same_machine(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-b1s@x.com", hostname="b1s")
    cust = await _customer(client, "c-b1-single@x.com")
    job_id = uuid.UUID(
        (await submit_job(client, cust, retry_policy={"max_retries": 1})).json()["job_id"]
    )

    assert (await claim(client, m["machine_id"], m["secret"])).status_code == 200
    await _expire_lease(job_id)
    record = next(r for r in await _recover() if r["job_id"] == str(job_id))

    # Refusing to retry would strand the job; retrying the same box is correct
    # here, but it must be reported honestly as NOT a failover.
    assert record["rescheduled"] is True
    assert record["failed_over"] is False
    assert await _job_machine(job_id) == uuid.UUID(m["machine_id"])


# ===========================================================================
# (c) + (d) retries exhausted -> terminal, hold released in full, no leak
# ===========================================================================
async def test_retries_exhausted_times_out_and_releases_full_hold(client):
    await seed_echo_model()
    a = await create_listable_machine(client, "p-b1x@x.com", hostname="b1x")
    b = await create_listable_machine(client, "p-b1y@x.com", hostname="b1y")
    cust = await _customer(client, "c-b1-exh@x.com")

    start = await _acct(client, cust)
    sub = await submit_job(
        client, cust, machine_id=a["machine_id"], retry_policy={"max_retries": 1}
    )
    job_id = uuid.UUID(sub.json()["job_id"])

    # Attempt 1 on A dies -> fails over to B.
    assert (await claim(client, a["machine_id"], a["secret"])).status_code == 200
    await _expire_lease(job_id)
    r1 = next(r for r in await _recover() if r["job_id"] == str(job_id))
    assert r1["machine_id"] == b["machine_id"]

    # Attempt 2 on B dies too -> retries exhausted.
    assert (await claim(client, b["machine_id"], b["secret"])).status_code == 200
    await _expire_lease(job_id)
    r2 = next(r for r in await _recover() if r["job_id"] == str(job_id))
    assert r2["action"] == "timed_out"

    got = (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust))).json()
    assert got["status"] == "timed_out"
    assert got["error_code"] == "timed_out"

    # (c) the ENTIRE hold is returned — a job that never produced output is
    # never charged.
    end = await _acct(client, cust)
    assert end["reserved"] == "0.00"
    assert end["balance"] == start["balance"]
    assert end["available"] == start["available"]
    assert await _txn_count(job_id, "capture") == 0

    # (d) both machines are free again.
    assert await _active_slots(a["machine_id"]) == 0
    assert await _active_slots(b["machine_id"]) == 0

    # Both failed attempts are on the record, one per machine.
    sm = get_sessionmaker()
    async with sm() as s:
        attempts = (
            await s.execute(
                select(JobAttempt).where(JobAttempt.job_id == job_id)
            )
        ).scalars().all()
    assert len(attempts) == 2
    assert {att.machine_id for att in attempts} == {
        uuid.UUID(a["machine_id"]),
        uuid.UUID(b["machine_id"]),
    }
    assert all(att.status == "lease_expired" for att in attempts)


# ===========================================================================
# a CANCELLED job is never retried
# ===========================================================================
async def test_cancelled_job_is_not_retried_and_hold_is_released(client):
    await seed_echo_model()
    a = await create_listable_machine(client, "p-b1c@x.com", hostname="b1c")
    await create_listable_machine(client, "p-b1d@x.com", hostname="b1d")
    cust = await _customer(client, "c-b1-cancel@x.com")

    start = await _acct(client, cust)
    sub = await submit_job(
        client, cust, machine_id=a["machine_id"], retry_policy={"max_retries": 3}
    )
    job_id = uuid.UUID(sub.json()["job_id"])
    assert (await claim(client, a["machine_id"], a["secret"])).status_code == 200

    # The customer cancels while the job is RUNNING and the provider then dies
    # before the cancel is observed (cancel_requested set, still RUNNING).
    sm = get_sessionmaker()
    async with sm() as s:
        job = await s.get(Job, job_id)
        job.cancel_requested = True
        job.lease_expires_at = utcnow() - dt.timedelta(seconds=10)
        await s.commit()

    record = next(r for r in await _recover() if r["job_id"] == str(job_id))
    assert record["action"] == "cancelled"

    got = (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust))).json()
    assert got["status"] == "cancelled"
    assert got["attempts_made"] == 0  # a cancellation never consumes a retry

    end = await _acct(client, cust)
    assert end["reserved"] == "0.00"
    assert end["balance"] == start["balance"]
    assert await _txn_count(job_id, "capture") == 0
    assert await _active_slots(a["machine_id"]) == 0


# ===========================================================================
# the dropped lease is attributed to the provider
# ===========================================================================
async def test_lease_expiry_records_reputation_event_for_failed_provider(client):
    await seed_echo_model()
    a = await create_listable_machine(client, "p-b1r@x.com", hostname="b1r")
    b = await create_listable_machine(client, "p-b1t@x.com", hostname="b1t")
    cust = await _customer(client, "c-b1-rep@x.com")
    job_id = uuid.UUID(
        (
            await submit_job(
                client, cust, machine_id=a["machine_id"], retry_policy={"max_retries": 1}
            )
        ).json()["job_id"]
    )

    assert (await claim(client, a["machine_id"], a["secret"])).status_code == 200
    await _expire_lease(job_id)
    await _recover()

    sm = get_sessionmaker()
    async with sm() as s:
        events = (
            await s.execute(
                select(ReputationEvent).where(ReputationEvent.job_id == job_id)
            )
        ).scalars().all()
    assert len(events) == 1
    assert events[0].kind == "lease_expired"
    # Attributed to the machine that dropped it, never to the rescue machine.
    assert events[0].machine_id == uuid.UUID(a["machine_id"])
    assert events[0].machine_id != uuid.UUID(b["machine_id"])
