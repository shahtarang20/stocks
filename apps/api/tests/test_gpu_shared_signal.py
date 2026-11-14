"""A MEASURED "this GPU was shared" signal on a job, instead of only the host's declared label."""

from __future__ import annotations

import uuid

import pytest

from app.models.enums import Role
from app.services.capability_flags import job_gpu_other_processes, job_gpu_shared, job_risk_note
from tests.helpers import auth_headers, grant_role, login, register_verify_login
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


# ---- what counts as a measurement -------------------------------------------------------------------------------
def test_only_a_real_true_counts_never_a_look_alike():
    assert job_gpu_shared({"gpu_shared": True}) is True
    for bad in ("true", 1, "yes", [], {}, None, 0, False):
        assert job_gpu_shared({"gpu_shared": bad}) is False, bad
    for not_a_dict in (None, "gpu_shared", [("gpu_shared", True)], 5):
        assert job_gpu_shared(not_a_dict) is False


def test_the_count_is_clamped_and_only_given_when_shared():
    assert job_gpu_other_processes({"gpu_shared": True, "gpu_other_processes": 3}) == 3
    assert job_gpu_other_processes({"gpu_shared": True, "gpu_other_processes": 10_000}) == 64
    assert job_gpu_other_processes({"gpu_shared": True, "gpu_other_processes": -5}) == 0
    assert job_gpu_other_processes({"gpu_shared": True, "gpu_other_processes": True}) is None  # a bool is not a count
    assert job_gpu_other_processes({"gpu_shared": True, "gpu_other_processes": "2"}) is None
    assert job_gpu_other_processes({"gpu_shared": True}) is None
    assert job_gpu_other_processes({"gpu_other_processes": 4}) is None  # a count without the flag is ignored


def test_the_note_says_what_was_measured_and_nothing_more():
    note = job_risk_note("dedicated", "completed", 0, True)
    assert note and "another program" in note and "same graphics card" in note and "slower" in note
    assert job_risk_note("dedicated", "completed", 0, False) is None
    both = job_risk_note("shared", "running", 1, True)
    assert "re-run on another host" in both and "another program" in both and "shared PC" in both


# ---- the whole path: agent result metrics -> job page and admin view ---------------------------------------------
async def _finished_job(client, tag, metrics):
    await seed_echo_model()
    machine = await create_listable_machine(client, f"p-{tag}@x.com", hostname=f"gs-{tag}")
    customer = await _customer(client, f"c-{tag}@x.com")
    job_id = (await submit_job(client, customer, machine_id=machine["machine_id"])).json()["job_id"]
    assert (await claim(client, machine["machine_id"], machine["secret"])).status_code == 200
    res = await post_result(
        client, machine["machine_id"], machine["secret"], job_id,
        {"status": "success", "execution_mode": "simulated", "output": {"text": "ok"}, "metrics": metrics},
    )
    assert res.status_code == 200, res.text
    return job_id, customer


async def test_a_measured_shared_gpu_is_shown_to_the_customer(client):
    job_id, customer = await _finished_job(
        client, "gs1", {"duration_seconds": 1.5, "gpu_shared": True, "gpu_other_processes": 2}
    )
    detail = (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(customer))).json()
    assert detail["gpu_shared"] is True
    assert "another program" in detail["risk_note"]
    assert detail["status"] == "completed"  # a shared GPU does not fail or cheapen the job: it is information


async def test_no_measurement_means_no_claim_either_way(client):
    job_id, customer = await _finished_job(client, "gs2", {"duration_seconds": 1.5})
    detail = (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(customer))).json()
    assert detail["gpu_shared"] is None  # not "False": absence is not proof of being alone on the card
    assert not detail["risk_note"] or "another program" not in detail["risk_note"]


async def test_a_host_cannot_make_up_a_look_alike_value(client):
    job_id, customer = await _finished_job(client, "gs3", {"gpu_shared": "true", "gpu_other_processes": "9"})
    detail = (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(customer))).json()
    assert detail["gpu_shared"] is None


async def test_the_admin_view_shows_it_with_the_count(client):
    job_id, _customer_token = await _finished_job(client, "gs4", {"gpu_shared": True, "gpu_other_processes": 3})
    await register_verify_login(client, "admin-gs@example.com")
    await grant_role("admin-gs@example.com", Role.administrator)
    admin = await login(client, "admin-gs@example.com")
    body = (await client.get(f"/v1/admin/jobs/{job_id}", headers=auth_headers(admin["access_token"]))).json()
    assert body["gpu_shared"] is True and body["gpu_other_processes"] == 3
