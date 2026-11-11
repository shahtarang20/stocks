"""Regression tests from the jobs / scheduling / data-plane bug audit.

Each test pins one defect that was verified against the code and fixed:

* a RUNNING job's capacity reservation expired after RESERVATION_TTL_SECONDS, so a long job freed its slot;
* an embeddings job (real hardware /v1/embeddings) always failed shape verification as ``unknown_workload``;
* an interactive session could be claimed by a banned or out-of-service machine;
* machines of a suspended / deleted provider kept receiving new jobs;
* one batch of hot telemetry samples raised one duplicate alert per sample (and the duplicates then broke ingest);
* the banned machine was still advertised on the public marketplace;
* a checkpoint step above the 32-bit column answered 500 on Postgres;
* job and batch lists were unbounded.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.enums import UserStatus
from app.models.jobs import Job
from app.models.marketplace import ResourceReservation
from app.models.provider import Provider, ProviderMachine
from app.models.telemetry import TelemetryAlert
from app.models.user import User
from app.services import verification
from tests.helpers import auth_headers
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import (
    _artifact_tmp,  # noqa: F401  (autouse fixture)
    _customer,
    agent_headers,
    claim,
    seed_echo_model,
    submit_job,
)

pytestmark = pytest.mark.asyncio


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


async def _reservation_of(job_id: uuid.UUID) -> ResourceReservation:
    async with get_sessionmaker()() as s:
        job = await s.get(Job, job_id)
        return await s.get(ResourceReservation, job.reservation_id)


async def _set_reservation_expiry(job_id: uuid.UUID, when: dt.datetime) -> None:
    async with get_sessionmaker()() as s:
        job = await s.get(Job, job_id)
        res = await s.get(ResourceReservation, job.reservation_id)
        res.expires_at = when
        await s.commit()


# --------------------------------------------------------------------------------------------
# running job keeps its capacity slot
# --------------------------------------------------------------------------------------------
async def test_claim_and_heartbeat_keep_running_jobs_reservation_alive(client):
    from app.services.marketplace import active_reservation_slots

    await seed_echo_model()
    m = await create_listable_machine(client, "p-resv@x.com", hostname="resv")
    cust = await _customer(client, "c-resv@x.com")
    job_id = uuid.UUID((await submit_job(client, cust)).json()["job_id"])
    machine_uuid = uuid.UUID(m["machine_id"])

    # The reservation is about to lapse when the agent claims: the claim must carry it forward.
    await _set_reservation_expiry(job_id, utcnow() + dt.timedelta(seconds=5))
    assert (await claim(client, m["machine_id"], m["secret"])).status_code == 200
    res = await _reservation_of(job_id)
    assert _aware(res.expires_at) > utcnow() + dt.timedelta(seconds=settings.RESERVATION_TTL_SECONDS - 60)

    # Much later (simulated by an already-lapsed expiry) the heartbeat renews it, so the machine is still full.
    await _set_reservation_expiry(job_id, utcnow() - dt.timedelta(seconds=1))
    async with get_sessionmaker()() as s:
        assert await active_reservation_slots(s, machine_uuid) == 0  # what a lapsed reservation does to capacity
    headers, raw = agent_headers(m["machine_id"], m["secret"], {})
    r = await client.post(f"/v1/agents/jobs/{job_id}/heartbeat", content=raw, headers=headers)
    assert r.status_code == 200, r.text
    async with get_sessionmaker()() as s:
        assert await active_reservation_slots(s, machine_uuid) == 1


async def test_extend_job_reservation_never_revives_a_released_reservation(client):
    from app.services.leases import extend_job_reservation

    await seed_echo_model()
    await create_listable_machine(client, "p-resv2@x.com", hostname="resv2")
    cust = await _customer(client, "c-resv2@x.com")
    job_id = uuid.UUID((await submit_job(client, cust)).json()["job_id"])
    async with get_sessionmaker()() as s:
        job = await s.get(Job, job_id)
        res = await s.get(ResourceReservation, job.reservation_id)
        res.status = "released"
        res.expires_at = utcnow() - dt.timedelta(seconds=30)
        await s.flush()
        await extend_job_reservation(s, job, utcnow())
        assert res.status == "released"
        assert _aware(res.expires_at) < utcnow()


# --------------------------------------------------------------------------------------------
# embeddings shape verification
# --------------------------------------------------------------------------------------------
async def test_verify_output_accepts_well_formed_embeddings():
    out = {"embeddings": [[0.1, 0.2], [0.3, -0.4]], "tokens": 4}
    v = verification.verify_output("embeddings", {"input": ["a", "b"]}, out)
    assert v["valid"] is True and v["reason"] is None


@pytest.mark.parametrize(
    "output",
    [
        {},  # no vectors
        {"embeddings": []},
        {"embeddings": [[]]},
        {"embeddings": [[0.1, "x"]]},
        {"embeddings": [[0.1, True]]},
        {"embeddings": [[0.1, float("nan")]]},
        {"embeddings": [[0.1, float("inf")]]},
        {"embeddings": [[0.1, 0.2]]},  # one vector for two inputs
        {"embeddings": "nope"},
    ],
)
async def test_verify_output_rejects_malformed_embeddings(output):
    v = verification.verify_output("embeddings", {"input": ["a", "b"]}, output)
    assert v["valid"] is False and v["reason"] == "malformed_embeddings"


async def test_verify_output_still_rejects_unknown_workload():
    assert verification.verify_output("nope", {}, {})["reason"] == "unknown_workload"


# --------------------------------------------------------------------------------------------
# session claim eligibility
# --------------------------------------------------------------------------------------------
def _pending_session(**over):
    from app.models.sessions import SESSION_PENDING, ComputeSession

    base = dict(
        customer_id=uuid.uuid4(), status=SESSION_PENDING, image_digest="x@sha256:" + "a" * 64, mode="pty",
        gpu_count=0, isolation_tier="container", max_duration_seconds=3600, idle_timeout_seconds=900,
        max_price_per_gpu_hour=1000, held_minor=0, client_pubkey="ab" * 32,
    )
    base.update(over)
    return ComputeSession(**base)


async def _latest_machine(s) -> ProviderMachine:
    return (await s.execute(select(ProviderMachine).order_by(ProviderMachine.created_at.desc()))).scalars().first()


@pytest.mark.parametrize(
    "field,value",
    [("is_banned", True), ("availability", "maintenance"), ("availability", "throttled"), ("availability", "offline")],
)
async def test_session_claim_refused_for_banned_or_out_of_service_machine(client, field, value):
    from app.models.sessions import SESSION_PENDING
    from app.services import sessions as svc

    await create_listable_machine(client, f"p-sess-{field}-{value}@x.com", hostname=f"s-{field}-{value}")
    async with get_sessionmaker()() as s:
        machine = await _latest_machine(s)
        row = _pending_session()
        s.add(row)
        await s.flush()
        setattr(machine, field, value)
        assert await svc.claim_next(s, machine, host_pubkey="cd" * 32, machine_gpus=1) is None
        await s.refresh(row)
        assert row.status == SESSION_PENDING and row.machine_id is None
        # control: the same machine claims it once it is eligible again
        setattr(machine, field, False if field == "is_banned" else "online")
        got = await svc.claim_next(s, machine, host_pubkey="cd" * 32, machine_gpus=1)
        assert got is not None and got[0].id == row.id


async def test_session_claim_refused_for_suspended_provider(client):
    from app.services import sessions as svc

    await create_listable_machine(client, "p-sess-susp@x.com", hostname="s-susp")
    async with get_sessionmaker()() as s:
        machine = await _latest_machine(s)
        provider = await s.get(Provider, machine.provider_id)
        (await s.get(User, provider.user_id)).status = UserStatus.suspended
        s.add(_pending_session())
        await s.flush()
        assert await svc.claim_next(s, machine, host_pubkey="cd" * 32, machine_gpus=1) is None


# --------------------------------------------------------------------------------------------
# suspended / deleted providers receive no new work
# --------------------------------------------------------------------------------------------
async def test_scheduler_skips_machines_of_a_suspended_provider(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-susp@x.com", hostname="susp")
    cust = await _customer(client, "c-susp@x.com")
    first = (await submit_job(client, cust)).json()
    assert first["status"] == "reserved" and first["machine_id"] == m["machine_id"]  # control: eligible when active

    async with get_sessionmaker()() as s:
        machine = await s.get(ProviderMachine, uuid.UUID(m["machine_id"]))
        provider = await s.get(Provider, machine.provider_id)
        (await s.get(User, provider.user_id)).status = UserStatus.suspended
        # free the slot the first job holds so only eligibility can explain the next outcome
        job = await s.get(Job, uuid.UUID(first["job_id"]))
        (await s.get(ResourceReservation, job.reservation_id)).status = "released"
        await s.commit()

    second = (await submit_job(client, cust)).json()
    assert second["status"] == "queued" and second["machine_id"] is None


async def test_scheduler_skips_machines_of_a_deleted_provider_row(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-del@x.com", hostname="del")
    cust = await _customer(client, "c-del@x.com")
    async with get_sessionmaker()() as s:
        machine = await s.get(ProviderMachine, uuid.UUID(m["machine_id"]))
        (await s.get(Provider, machine.provider_id)).status = "deleted"
        await s.commit()
    assert (await submit_job(client, cust)).json()["status"] == "queued"


# --------------------------------------------------------------------------------------------
# telemetry alert de-duplication inside one batch
# --------------------------------------------------------------------------------------------
async def test_hot_telemetry_batch_raises_one_alert_and_ingest_keeps_working(client):
    from app.schemas.telemetry import MachineTelemetryItem
    from app.services.telemetry import record_machine_telemetry

    m = await create_listable_machine(client, "p-tele@x.com", hostname="tele")
    machine_id = uuid.UUID(m["machine_id"])
    hot = settings.ALERT_TEMP_THRESHOLD_C + 1
    points = [MachineTelemetryItem(gpu_index=0, temp_c=hot, gpu_util_pct=50.0) for _ in range(5)]
    async with get_sessionmaker()() as s:
        stored, alerts = await record_machine_telemetry(s, machine_id, points)
        await s.commit()
    assert stored == 5 and alerts == 1
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(TelemetryAlert).where(TelemetryAlert.machine_id == machine_id))).scalars().all()
        assert len(rows) == 1
        # a second batch inside the window raises nothing more (and does not blow up)
        stored2, alerts2 = await record_machine_telemetry(s, machine_id, points[:2])
        assert stored2 == 2 and alerts2 == 0


async def test_telemetry_ingest_survives_pre_existing_duplicate_alerts(client):
    """Duplicates written by the old behaviour made ``scalar_one_or_none`` raise MultipleResultsFound (500)."""
    from app.schemas.telemetry import MachineTelemetryItem
    from app.services.telemetry import record_machine_telemetry

    m = await create_listable_machine(client, "p-tele2@x.com", hostname="tele2")
    machine_id = uuid.UUID(m["machine_id"])
    async with get_sessionmaker()() as s:
        for _ in range(2):
            s.add(TelemetryAlert(machine_id=machine_id, alert_type="high_temperature", severity="warning", message="x",
                                 metric_value=90.0, threshold=80.0, status="active", created_at=utcnow()))
        await s.commit()
    async with get_sessionmaker()() as s:
        stored, alerts = await record_machine_telemetry(
            s, machine_id, [MachineTelemetryItem(gpu_index=0, temp_c=settings.ALERT_TEMP_THRESHOLD_C + 5)]
        )
        assert stored == 1 and alerts == 0


# --------------------------------------------------------------------------------------------
# marketplace listing
# --------------------------------------------------------------------------------------------
async def test_banned_machine_is_not_listed_on_the_marketplace(client):
    m = await create_listable_machine(client, "p-ban@x.com", hostname="ban")
    listed = (await client.get("/v1/marketplace/machines")).json()
    assert m["machine_id"] in [i["machine_id"] for i in listed["items"]]
    async with get_sessionmaker()() as s:
        (await s.get(ProviderMachine, uuid.UUID(m["machine_id"]))).is_banned = True
        await s.commit()
    listed = (await client.get("/v1/marketplace/machines")).json()
    assert m["machine_id"] not in [i["machine_id"] for i in listed["items"]]
    assert (await client.get(f"/v1/marketplace/machines/{m['machine_id']}")).status_code == 404


# --------------------------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------------------------
async def test_checkpoint_step_must_fit_the_32_bit_column(client):
    from app.services import datasets as ds

    await seed_echo_model()
    await create_listable_machine(client, "p-ck@x.com", hostname="ck")
    cust = await _customer(client, "c-ck@x.com")
    job_id = (await submit_job(client, cust)).json()["job_id"]
    token = ds.make_grant(ds.SCOPE_CHECKPOINT_UPLOAD, job_id, utcnow() + dt.timedelta(minutes=5))
    big = await client.put(f"/v1/training/checkpoints/upload/{token}", content=b"x", headers={"X-Checkpoint-Step": str(2**40)})
    assert big.status_code == 422 and big.json()["detail"] == "invalid_step"
    ok = await client.put(f"/v1/training/checkpoints/upload/{token}", content=b"x", headers={"X-Checkpoint-Step": "2147483647"})
    assert ok.status_code == 201, ok.text


async def test_job_and_batch_lists_are_bounded_and_paginated(client):
    await seed_echo_model()
    await create_listable_machine(client, "p-list@x.com", hostname="list", max_concurrent_jobs=5)
    cust = await _customer(client, "c-list@x.com")
    h = auth_headers(cust)
    ids = [(await submit_job(client, cust)).json()["job_id"] for _ in range(3)]
    assert len((await client.get("/v1/jobs", headers=h)).json()) == 3
    one = await client.get("/v1/jobs?limit=1", headers=h)
    assert len(one.json()) == 1
    rest = await client.get("/v1/jobs?limit=5&offset=1", headers=h)
    assert len(rest.json()) == 2
    assert {j["job_id"] for j in one.json() + rest.json()} == set(ids)
    assert (await client.get("/v1/jobs?limit=0", headers=h)).status_code == 422
    assert (await client.get("/v1/jobs?limit=100000", headers=h)).status_code == 422
    assert (await client.get("/v1/batches?limit=0", headers=h)).status_code == 422
    assert (await client.get("/v1/batches?limit=1", headers=h)).status_code == 200


@pytest.mark.parametrize("value", ["1e400", "1e-400", "1e20", "1000000000000"])
async def test_job_create_max_cost_is_bounded_to_the_column(value):
    from pydantic import ValidationError

    from app.schemas.jobs import JobCreate

    with pytest.raises(ValidationError):
        JobCreate(workload_type="text-generation", model="m", max_cost=value)
    assert JobCreate(workload_type="text-generation", model="m", max_cost="999999999999.99").max_cost is not None
