"""A host's own usage numbers: live readings and how the last days were spent. Owner only; nothing about customers."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.jobs import JobAttempt
from app.models.telemetry import MachineTelemetry
from app.services.machine_usage import (
    REPORTING_BUCKET_SECONDS,
    attempt_outcome,
    clip,
    reporting_seconds,
    union_seconds,
)
from tests.helpers import auth_headers
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import (  # noqa: F401  (_artifact_tmp is an autouse fixture)
    _artifact_tmp,
    _customer,
    claim,
    seed_echo_model,
    submit_job,
)

T0 = dt.datetime(2026, 11, 14, 12, 0, 0, tzinfo=dt.timezone.utc)


def _t(minutes: float) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


# ===== pure helpers ====================================================================================================
def test_union_counts_overlapping_time_once():
    assert union_seconds([(_t(0), _t(30)), (_t(10), _t(40))]) == 40 * 60  # two jobs at once are not double counted
    assert union_seconds([(_t(0), _t(10)), (_t(20), _t(30))]) == 20 * 60  # a gap stays a gap
    assert union_seconds([(_t(0), _t(60)), (_t(10), _t(20))]) == 60 * 60  # one inside another
    assert union_seconds([]) == 0
    assert union_seconds([(_t(5), _t(5)), (_t(9), _t(3))]) == 0  # empty and backwards spans count for nothing


def test_clip_cuts_to_the_window_and_drops_what_is_outside():
    assert clip(_t(-30), _t(30), _t(0), _t(60)) == (_t(0), _t(30))
    assert clip(_t(30), _t(90), _t(0), _t(60)) == (_t(30), _t(60))
    assert clip(_t(70), _t(80), _t(0), _t(60)) is None


def test_a_five_minute_slice_with_any_reading_counts_in_full_and_slices_are_counted_once():
    base = int(T0.timestamp()) // REPORTING_BUCKET_SECONDS * REPORTING_BUCKET_SECONDS
    stamps = [dt.datetime.fromtimestamp(base + s, tz=dt.timezone.utc) for s in (1, 2, 30, 299, 301, 700)]  # 3 slices
    assert reporting_seconds(stamps, _t(-60), _t(60)) == 3 * REPORTING_BUCKET_SECONDS


def test_reporting_can_never_exceed_the_window():
    base = int(T0.timestamp()) // REPORTING_BUCKET_SECONDS * REPORTING_BUCKET_SECONDS
    stamps = [dt.datetime.fromtimestamp(base + i * REPORTING_BUCKET_SECONDS, tz=dt.timezone.utc) for i in range(10)]
    assert reporting_seconds(stamps, _t(0), _t(5)) == 5 * 60


def test_attempt_outcomes_are_named_for_a_host():
    assert attempt_outcome("completed", None) == "completed"
    assert attempt_outcome("running", None) == "running"
    assert attempt_outcome("cancelled", None) == "cancelled"
    assert attempt_outcome("lease_expired", "lease_expired") == "lost"
    assert attempt_outcome("runtime_exceeded", "runtime_exceeded") == "too_long"
    assert attempt_outcome("failed", "thermal_shutdown") == "stopped_here"
    assert attempt_outcome("failed", "host_interrupted") == "stopped_here"
    assert attempt_outcome("failed", "oom") == "failed"  # the customer's own code failing is not the host's doing
    assert attempt_outcome("failed", None) == "failed"
    assert attempt_outcome("something_new", None) == "other"


# ===== the API =========================================================================================================
async def _machine_with_job(client, tag):
    await seed_echo_model()
    machine = await create_listable_machine(client, f"p-{tag}@x.com", hostname=f"usage-{tag}")
    customer = await _customer(client, f"c-{tag}@x.com")
    sub = await submit_job(client, customer, machine_id=machine["machine_id"])
    job_id = uuid.UUID(sub.json()["job_id"])
    return machine, customer, job_id


async def _add_readings(machine_id, stamps, gpu=0, util=50.0, temp=60.0):
    async with get_sessionmaker()() as s:
        for ts in stamps:
            s.add(MachineTelemetry(
                machine_id=uuid.UUID(machine_id), timestamp=ts, gpu_index=gpu, gpu_util_pct=util, mem_util_pct=20.0,
                mem_used_mb=2000 + gpu, mem_total_mb=8192, temp_c=temp, power_w=100.0,
            ))
        await s.commit()


async def _add_attempt(job_id, machine_id, start, end, status, error_code=None, number=10):
    async with get_sessionmaker()() as s:
        s.add(JobAttempt(
            job_id=job_id, attempt_number=number, machine_id=uuid.UUID(machine_id), status=status, error_code=error_code,
            started_at=start, finished_at=end,
        ))
        await s.commit()


@pytest.mark.asyncio
async def test_live_with_no_readings_says_so_instead_of_showing_old_or_invented_numbers(client):
    machine, _c, _j = await _machine_with_job(client, "mu1")
    res = await client.get(f"/provider/machines/{machine['machine_id']}/live", headers=auth_headers(machine["access"]))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["readings_recent"] is False and body["gpus"] == [] and body["utilization_last_hour"] == []
    assert body["status"] == "online" and body["hostname"] == "usage-mu1"
    assert body["last_reading_at"] is None and body["running_jobs"] == 0


@pytest.mark.asyncio
async def test_live_shows_the_newest_reading_of_each_card_and_the_last_hour(client):
    machine, _c, _j = await _machine_with_job(client, "mu2")
    now = utcnow()
    await _add_readings(machine["machine_id"], [now - dt.timedelta(seconds=200)], gpu=0, util=10.0)  # older
    await _add_readings(machine["machine_id"], [now - dt.timedelta(seconds=20)], gpu=0, util=90.0)  # newest of card 0
    await _add_readings(machine["machine_id"], [now - dt.timedelta(seconds=25)], gpu=1, util=33.0)
    await _add_readings(machine["machine_id"], [now - dt.timedelta(minutes=40)], gpu=0, util=70.0, temp=77.0)
    res = await client.get(f"/provider/machines/{machine['machine_id']}/live", headers=auth_headers(machine["access"]))
    body = res.json()
    assert body["readings_recent"] is True
    assert [(g["gpu_index"], g["gpu_util_pct"]) for g in body["gpus"]] == [(0, 90.0), (1, 33.0)]
    assert body["gpus"][0]["mem_total_mb"] == 8192 and body["gpus"][0]["temp_c"] == 60.0
    assert len(body["utilization_last_hour"]) >= 2  # the 40-minute-old reading is in the hour's chart
    assert max(p["max_temp_c"] for p in body["utilization_last_hour"]) == 77.0


@pytest.mark.asyncio
async def test_old_readings_are_not_shown_as_live_gauges(client):
    machine, _c, _j = await _machine_with_job(client, "mu3")
    await _add_readings(machine["machine_id"], [utcnow() - dt.timedelta(minutes=30)])
    body = (await client.get(f"/provider/machines/{machine['machine_id']}/live", headers=auth_headers(machine["access"]))).json()
    assert body["gpus"] == [] and body["readings_recent"] is False  # 30 minutes old is history, not "live"
    assert body["last_reading_at"] is not None


@pytest.mark.asyncio
async def test_live_counts_the_jobs_running_right_now(client):
    machine, _c, _j = await _machine_with_job(client, "mu4")
    assert (await claim(client, machine["machine_id"], machine["secret"])).status_code == 200
    body = (await client.get(f"/provider/machines/{machine['machine_id']}/live", headers=auth_headers(machine["access"]))).json()
    assert body["running_jobs"] == 1


@pytest.mark.asyncio
async def test_usage_without_any_readings_does_not_pretend_to_know_idle_or_offline(client):
    machine, _c, job_id = await _machine_with_job(client, "mu5")
    now = utcnow()
    await _add_attempt(job_id, machine["machine_id"], now - dt.timedelta(hours=2), now - dt.timedelta(hours=1), "completed")
    res = await client.get(f"/provider/machines/{machine['machine_id']}/usage", headers=auth_headers(machine["access"]))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["telemetry_available"] is False
    assert body["seconds"]["idle"] is None and body["seconds"]["not_reporting"] is None
    assert body["seconds"]["running"] == 3600  # the jobs themselves are real data


@pytest.mark.asyncio
async def test_usage_splits_the_period_into_running_idle_and_not_reporting(client):
    machine, _c, job_id = await _machine_with_job(client, "mu6")
    now = utcnow()
    start = now - dt.timedelta(hours=3)
    base = int(start.timestamp()) // 300 * 300
    # 12 five-minute slices with a reading = one hour "there"; a 30-minute job happened inside it
    await _add_readings(machine["machine_id"], [dt.datetime.fromtimestamp(base + i * 300 + 10, tz=dt.timezone.utc) for i in range(12)])
    await _add_attempt(job_id, machine["machine_id"], start + dt.timedelta(minutes=5), start + dt.timedelta(minutes=35), "completed")

    body = (await client.get(f"/provider/machines/{machine['machine_id']}/usage?days=1", headers=auth_headers(machine["access"]))).json()
    s = body["seconds"]
    assert body["telemetry_available"] is True and body["days"] == 1
    assert s["window"] == 86400
    assert s["running"] == 1800
    assert s["idle"] == 3600 - 1800
    assert s["not_reporting"] == 86400 - 3600
    assert s["running"] + s["idle"] + s["not_reporting"] == s["window"]  # the three always add up to the whole period


@pytest.mark.asyncio
async def test_two_jobs_at_once_are_counted_once_and_outcomes_are_named_for_the_host(client):
    machine, _c, job_id = await _machine_with_job(client, "mu7")
    now = utcnow()
    s0 = now - dt.timedelta(hours=2)
    await _add_readings(machine["machine_id"], [s0 + dt.timedelta(minutes=i * 5) for i in range(20)])
    await _add_attempt(job_id, machine["machine_id"], s0, s0 + dt.timedelta(minutes=30), "completed", number=11)
    await _add_attempt(job_id, machine["machine_id"], s0 + dt.timedelta(minutes=10), s0 + dt.timedelta(minutes=40),
                       "failed", "thermal_shutdown", number=12)
    await _add_attempt(job_id, machine["machine_id"], s0 + dt.timedelta(minutes=50), s0 + dt.timedelta(minutes=55),
                       "lease_expired", "lease_expired", number=13)
    await _add_attempt(job_id, machine["machine_id"], s0 + dt.timedelta(minutes=60), s0 + dt.timedelta(minutes=65),
                       "failed", "oom", number=14)
    body = (await client.get(f"/provider/machines/{machine['machine_id']}/usage", headers=auth_headers(machine["access"]))).json()
    assert body["seconds"]["running"] == (40 + 5 + 5) * 60  # 0-40 (two overlapping jobs, once), 50-55, 60-65
    assert body["attempts"] == {"completed": 1, "stopped_here": 1, "lost": 1, "failed": 1}


@pytest.mark.asyncio
async def test_the_period_is_limited_to_one_to_fourteen_days(client):
    machine, _c, _j = await _machine_with_job(client, "mu8")
    h = auth_headers(machine["access"])
    base = f"/provider/machines/{machine['machine_id']}/usage"
    assert (await client.get(base + "?days=0", headers=h)).status_code == 422
    assert (await client.get(base + "?days=15", headers=h)).status_code == 422
    assert (await client.get(base + "?days=14", headers=h)).status_code == 200


# ===== who may see it ================================================================================================
@pytest.mark.asyncio
async def test_only_the_owner_can_see_a_computers_numbers(client):
    machine, customer, _j = await _machine_with_job(client, "mu9")
    other = await create_listable_machine(client, "p-mu9-other@x.com", hostname="someone-elses")
    for path in ("live", "usage"):
        url = f"/provider/machines/{machine['machine_id']}/{path}"
        assert (await client.get(url, headers=auth_headers(other["access"]))).status_code == 404  # another host: not found
        assert (await client.get(url, headers=auth_headers(customer))).status_code == 403  # a customer is not a host
        client.cookies.clear()  # the test client keeps login cookies: really be signed out
        assert (await client.get(url)).status_code in (401, 403)  # signed out
        assert (await client.get(url, headers=auth_headers(machine["access"]))).status_code == 200


@pytest.mark.asyncio
async def test_nothing_about_customers_or_their_jobs_is_in_what_a_host_sees(client):
    machine, customer, job_id = await _machine_with_job(client, "mu10")
    now = utcnow()
    await _add_attempt(job_id, machine["machine_id"], now - dt.timedelta(hours=1), now - dt.timedelta(minutes=30), "completed")
    h = auth_headers(machine["access"])
    for path in ("live", "usage"):
        text = (await client.get(f"/provider/machines/{machine['machine_id']}/{path}", headers=h)).text
        assert str(job_id) not in text
        assert "c-mu10@x.com" not in text and "customer" not in text.lower()
