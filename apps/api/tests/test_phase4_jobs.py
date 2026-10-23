"""Phase 4 — job submission pipeline, agent protocol, artifacts, leases, isolation."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.jobs import Job
from app.models.marketplace import ResourceReservation
from app.models.model_registry import Model, ModelVersion
from app.services import storage
from tests.helpers import auth_headers, register_verify_login
from tests.test_phase3_marketplace import create_listable_machine

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _artifact_tmp(tmp_path):
    """Point object storage at a temp dir and shrink the size cap for tests."""
    old_dir = settings.OBJECT_STORAGE_LOCAL_DIR
    old_max = settings.ARTIFACT_MAX_BYTES
    settings.OBJECT_STORAGE_LOCAL_DIR = str(tmp_path / "artifacts")
    settings.ARTIFACT_MAX_BYTES = 1024
    storage.reset_store()
    yield
    settings.OBJECT_STORAGE_LOCAL_DIR = old_dir
    settings.ARTIFACT_MAX_BYTES = old_max
    storage.reset_store()


async def seed_echo_model() -> None:
    """Seed the approved simulated model (migrations are not run in tests)."""
    sm = get_sessionmaker()
    async with sm() as s:
        existing = (
            await s.execute(select(ModelVersion).where(ModelVersion.version_tag == "echo-sim-v1"))
        ).scalar_one_or_none()
        if existing:
            return
        model = Model(name="echo-sim", display_name="Echo (Simulated)", trust_status="approved")
        s.add(model)
        await s.flush()
        s.add(
            ModelVersion(
                model_id=model.id,
                version_tag="echo-sim-v1",
                runtime="simulated",
                supported_workloads=["text-generation", "batch-inference"],
                trust_status="approved",
            )
        )
        await s.commit()


async def _customer(client: AsyncClient, email: str) -> str:
    tokens = await register_verify_login(client, email)
    return tokens["access_token"]


def agent_headers(machine_id: str, secret: str, body: dict) -> tuple[dict, bytes]:
    raw = json.dumps(body).encode("utf-8")
    ts = str(int(utcnow().timestamp()))
    signing_key = secret.removeprefix("msk_")
    canonical = f"{machine_id}\n{ts}\n{hashlib.sha256(raw).hexdigest()}"
    sig = hmac.new(signing_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-ComputeCoin-Machine": machine_id,
        "X-ComputeCoin-Timestamp": ts,
        "X-ComputeCoin-Signature": sig,
        "Content-Type": "application/json",
    }
    return headers, raw


async def submit_job(client: AsyncClient, token: str, *, key: str | None = None, **overrides):
    body = {
        "workload_type": "text-generation",
        "model": "echo-sim-v1",
        "input": {"prompt": "Hello"},
        "gpu_count": 1,
        "estimated_minutes": 5,
        "max_runtime_seconds": 300,
        "retry_policy": {"max_retries": 0},
    }
    body.update(overrides)
    headers = auth_headers(token)
    if key:
        headers["Idempotency-Key"] = key
    return await client.post("/v1/jobs", json=body, headers=headers)


async def claim(client: AsyncClient, machine_id: str, secret: str):
    headers, raw = agent_headers(machine_id, secret, {"machine_id": machine_id})
    return await client.post("/v1/agents/jobs/claim", content=raw, headers=headers)


async def post_result(client, machine_id, secret, job_id, payload):
    headers, raw = agent_headers(machine_id, secret, payload)
    return await client.post(f"/v1/agents/jobs/{job_id}/result", content=raw, headers=headers)


# ===========================================================================
# submit pipeline
# ===========================================================================
async def test_submit_text_generation_reaches_reserved_with_match_explanation(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-tg@x.com", hostname="tg")
    cust = await _customer(client, "c-tg@x.com")
    r = await submit_job(client, cust)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "reserved"
    assert body["execution_mode"] == "simulated"
    assert body["machine_id"] == m["machine_id"]
    assert body["reservation_id"] is not None
    assert body["match_explanation"]["chosen_machine_id"] == m["machine_id"]
    assert "factors" in body["match_explanation"]


async def test_submit_batch_inference_reaches_reserved(client):
    await seed_echo_model()
    await create_listable_machine(client, "p-bi@x.com", hostname="bi")
    cust = await _customer(client, "c-bi@x.com")
    r = await submit_job(
        client, cust, workload_type="batch-inference", input={"items": [1, 2, 3]}
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "reserved"


async def test_submit_idempotency_same_key_returns_same_job(client):
    await seed_echo_model()
    await create_listable_machine(client, "p-id@x.com", hostname="id", max_concurrent_jobs=2)
    cust = await _customer(client, "c-id@x.com")
    r1 = await submit_job(client, cust, key="abc-123")
    r2 = await submit_job(client, cust, key="abc-123")
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["job_id"] == r2.json()["job_id"]
    # Only one job persisted for this customer.
    all_jobs = await client.get("/v1/jobs", headers=auth_headers(cust))
    assert len(all_jobs.json()) == 1


# ===========================================================================
# validation 422 / 409
# ===========================================================================
async def test_validation_model_not_supported_422(client):
    await seed_echo_model()
    cust = await _customer(client, "c-v1@x.com")
    r = await submit_job(client, cust, model="does-not-exist")
    assert r.status_code == 422
    assert r.json()["detail"] == "model_not_supported"


async def test_validation_invalid_input_422(client):
    await seed_echo_model()
    cust = await _customer(client, "c-v2@x.com")
    r = await submit_job(client, cust, input={"not_a_prompt": 1})
    assert r.status_code == 422
    assert r.json()["detail"] == "invalid_input"


async def test_validation_gpu_exceeds_machine_422(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-v3@x.com", hostname="v3")  # 1 gpu
    cust = await _customer(client, "c-v3@x.com")
    r = await submit_job(client, cust, machine_id=m["machine_id"], gpu_count=4)
    assert r.status_code == 422
    assert r.json()["detail"] == "gpu_count_exceeds_machine"


async def test_validation_budget_exceeded_409(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-v4@x.com", hostname="v4")
    cust = await _customer(client, "c-v4@x.com")
    r = await submit_job(client, cust, machine_id=m["machine_id"], max_cost="0.01")
    assert r.status_code == 409
    assert r.json()["detail"] == "budget_exceeded"


# ===========================================================================
# no provider -> QUEUED then FAILED
# ===========================================================================
async def test_no_provider_stays_queued_then_fails_after_retries(client):
    await seed_echo_model()
    cust = await _customer(client, "c-np@x.com")
    r = await submit_job(client, cust)  # no machines exist
    assert r.status_code == 201
    assert r.json()["status"] == "queued"
    job_id = uuid.UUID(r.json()["job_id"])

    # Exhaust scheduling retries (max_retries=0 -> first failure -> FAILED).
    from app.services.scheduler import run_scheduler

    sm = get_sessionmaker()
    async with sm() as s:
        job = await s.get(Job, job_id)
        await run_scheduler(s, job)
        await s.commit()

    got = await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust))
    assert got.json()["status"] == "failed"
    assert got.json()["error_code"] == "no_provider"


# ===========================================================================
# agent claim
# ===========================================================================
async def test_claim_reserved_to_running_and_claim_once(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-cl@x.com", hostname="cl")
    cust = await _customer(client, "c-cl@x.com")
    sub = await submit_job(client, cust)
    job_id = sub.json()["job_id"]

    first = await claim(client, m["machine_id"], m["secret"])
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["job_id"] == job_id
    assert data["lease_expires_at"] is not None
    assert data["artifact_upload"]["token"]
    assert data["image_digest"] is None  # simulated

    # Job is now RUNNING with a lease.
    got = await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust))
    assert got.json()["status"] == "running"
    assert got.json()["lease_expires_at"] is not None

    # Claim-once: a second claim finds no RESERVED job.
    second = await claim(client, m["machine_id"], m["secret"])
    assert second.status_code == 204


async def test_claim_bad_hmac_401(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-bh@x.com", hostname="bh")
    cust = await _customer(client, "c-bh@x.com")
    await submit_job(client, cust)
    headers, raw = agent_headers(m["machine_id"], m["secret"], {"machine_id": m["machine_id"]})
    headers["X-ComputeCoin-Signature"] = "deadbeef"
    resp = await client.post("/v1/agents/jobs/claim", content=raw, headers=headers)
    assert resp.status_code == 401


async def test_cross_machine_claim_blocked(client):
    await seed_echo_model()
    m_a = await create_listable_machine(client, "p-ca@x.com", hostname="ca")
    m_b = await create_listable_machine(client, "p-cb@x.com", hostname="cb")
    cust = await _customer(client, "c-cm@x.com")
    # Target machine A so the job is reserved to A.
    sub = await submit_job(client, cust, machine_id=m_a["machine_id"])
    assert sub.json()["machine_id"] == m_a["machine_id"]

    # Machine B cannot claim A's job.
    b_claim = await claim(client, m_b["machine_id"], m_b["secret"])
    assert b_claim.status_code == 204
    # Machine A can.
    a_claim = await claim(client, m_a["machine_id"], m_a["secret"])
    assert a_claim.status_code == 200


# ===========================================================================
# result: success / failed / idempotent
# ===========================================================================
async def test_result_success_completes_with_verification_and_releases_reservation(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-rs@x.com", hostname="rs")
    cust = await _customer(client, "c-rs@x.com")
    sub = await submit_job(client, cust)
    job_id = sub.json()["job_id"]
    claim_resp = (await claim(client, m["machine_id"], m["secret"])).json()

    # Upload an output artifact via the signed grant.
    token = claim_resp["artifact_upload"]["token"]
    up = await client.put(
        f"/v1/artifacts/upload/{token}",
        content=b'{"text": "Hello world"}',
        headers={"Content-Type": "application/json"},
    )
    assert up.status_code == 201, up.text
    artifact_id = up.json()["artifact_id"]

    result = await post_result(
        client, m["machine_id"], m["secret"], job_id,
        {
            "status": "success",
            "execution_mode": "simulated",
            "output": {"text": "Hello world", "simulated": True},
            "artifacts": [{"artifact_id": artifact_id, "kind": "output"}],
            "metrics": {"duration_seconds": 0.2},
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed"

    got = (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust))).json()
    assert got["status"] == "completed"
    assert got["verification"]["valid"] is True
    assert got["output"]["text"] == "Hello world"

    # Reservation released -> capacity restored.
    search = await client.get(f"/v1/marketplace/machines/{m['machine_id']}")
    assert search.json()["available_slots"] == 1

    # Artifact metadata listed.
    arts = (await client.get(f"/v1/jobs/{job_id}/artifacts", headers=auth_headers(cust))).json()
    assert len(arts) == 1
    assert arts[0]["artifact_id"] == artifact_id
    assert arts[0]["content_type"] == "application/json"


async def test_result_failed_sets_failed_and_releases(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-rf@x.com", hostname="rf")
    cust = await _customer(client, "c-rf@x.com")
    job_id = (await submit_job(client, cust)).json()["job_id"]
    await claim(client, m["machine_id"], m["secret"])

    result = await post_result(
        client, m["machine_id"], m["secret"], job_id,
        {"status": "failed", "execution_mode": "simulated",
         "error": {"code": "oom", "message": "out of memory"}},
    )
    assert result.status_code == 200
    assert result.json()["status"] == "failed"
    got = (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust))).json()
    assert got["status"] == "failed"
    assert got["error_code"] == "oom"
    search = await client.get(f"/v1/marketplace/machines/{m['machine_id']}")
    assert search.json()["available_slots"] == 1


async def test_result_idempotent(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-ri@x.com", hostname="ri")
    cust = await _customer(client, "c-ri@x.com")
    job_id = (await submit_job(client, cust)).json()["job_id"]
    await claim(client, m["machine_id"], m["secret"])
    payload = {"status": "success", "execution_mode": "simulated", "output": {"text": "ok"}}
    r1 = await post_result(client, m["machine_id"], m["secret"], job_id, payload)
    r2 = await post_result(client, m["machine_id"], m["secret"], job_id, payload)
    assert r1.json()["status"] == "completed"
    assert r2.status_code == 200
    assert r2.json()["status"] == "completed"


async def test_result_verification_mismatch_batch_fails(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-vm@x.com", hostname="vm")
    cust = await _customer(client, "c-vm@x.com")
    job_id = (
        await submit_job(client, cust, workload_type="batch-inference", input={"items": [1, 2, 3]})
    ).json()["job_id"]
    await claim(client, m["machine_id"], m["secret"])
    # Manifest has only 1 item but input had 3 -> verification fails -> FAILED.
    result = await post_result(
        client, m["machine_id"], m["secret"], job_id,
        {"status": "success", "execution_mode": "simulated",
         "output": {"manifest": [{"status": "ok"}]}},
    )
    assert result.json()["status"] == "failed"
    got = (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust))).json()
    assert got["verification"]["valid"] is False


# ===========================================================================
# logs
# ===========================================================================
async def test_logs_append_and_owner_retrieve(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-lg@x.com", hostname="lg")
    cust = await _customer(client, "c-lg@x.com")
    job_id = (await submit_job(client, cust)).json()["job_id"]
    await claim(client, m["machine_id"], m["secret"])

    headers, raw = agent_headers(
        m["machine_id"], m["secret"], {"lines": ["starting", "working", "done"]}
    )
    append = await client.post(f"/v1/agents/jobs/{job_id}/logs", content=raw, headers=headers)
    assert append.status_code == 200, append.text

    logs = await client.get(f"/v1/jobs/{job_id}/logs", headers=auth_headers(cust))
    assert logs.status_code == 200
    assert logs.json()["lines"] == ["starting", "working", "done"]


# ===========================================================================
# artifacts upload/download + rejection
# ===========================================================================
async def test_artifact_download_owner_and_non_owner_404(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-ar@x.com", hostname="ar")
    cust = await _customer(client, "c-ar@x.com")
    other = await _customer(client, "c-ar-other@x.com")
    job_id = (await submit_job(client, cust)).json()["job_id"]
    claim_resp = (await claim(client, m["machine_id"], m["secret"])).json()
    token = claim_resp["artifact_upload"]["token"]
    up = await client.put(
        f"/v1/artifacts/upload/{token}",
        content=b'{"text": "result"}',
        headers={"Content-Type": "application/json"},
    )
    artifact_id = up.json()["artifact_id"]

    # Owner gets a signed URL and can download the bytes.
    dl = await client.get(
        f"/v1/jobs/{job_id}/artifacts/{artifact_id}/download", headers=auth_headers(cust)
    )
    assert dl.status_code == 200
    signed_url = dl.json()["url"]
    content = await client.get(signed_url)
    assert content.status_code == 200
    assert content.content == b'{"text": "result"}'

    # Non-owner cannot even obtain the signed URL.
    dl_other = await client.get(
        f"/v1/jobs/{job_id}/artifacts/{artifact_id}/download", headers=auth_headers(other)
    )
    assert dl_other.status_code == 404


async def test_artifact_oversized_and_bad_content_type_rejected(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-ax@x.com", hostname="ax")
    cust = await _customer(client, "c-ax@x.com")
    await submit_job(client, cust)
    token = (await claim(client, m["machine_id"], m["secret"])).json()["artifact_upload"]["token"]

    # Bad content type -> 415.
    bad_ct = await client.put(
        f"/v1/artifacts/upload/{token}",
        content=b"zipbytes",
        headers={"Content-Type": "application/zip"},
    )
    assert bad_ct.status_code == 415

    # Oversized (> 1024-byte test cap) -> 413.
    big = await client.put(
        f"/v1/artifacts/upload/{token}",
        content=b"x" * 2048,
        headers={"Content-Type": "text/plain"},
    )
    assert big.status_code == 413


# ===========================================================================
# cancel
# ===========================================================================
async def test_cancel_releases_reservation(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-cn@x.com", hostname="cn")
    cust = await _customer(client, "c-cn@x.com")
    job_id = (await submit_job(client, cust)).json()["job_id"]

    cancel = await client.post(f"/v1/jobs/{job_id}/cancel", json={}, headers=auth_headers(cust))
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"
    assert cancel.json()["cancel_requested"] is True

    search = await client.get(f"/v1/marketplace/machines/{m['machine_id']}")
    assert search.json()["available_slots"] == 1


# ===========================================================================
# lease recovery
# ===========================================================================
async def _expire_lease(job_id: uuid.UUID) -> None:
    sm = get_sessionmaker()
    async with sm() as s:
        job = await s.get(Job, job_id)
        job.lease_expires_at = utcnow() - dt.timedelta(seconds=10)
        await s.commit()


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


async def test_lease_recovery_requeues_then_times_out(client):
    from app.services.leases import recover_expired_leases

    await seed_echo_model()
    m = await create_listable_machine(client, "p-lr@x.com", hostname="lr")
    cust = await _customer(client, "c-lr@x.com")
    job_id = uuid.UUID(
        (await submit_job(client, cust, retry_policy={"max_retries": 1})).json()["job_id"]
    )

    # Attempt 1: claim then expire the lease.
    assert (await claim(client, m["machine_id"], m["secret"])).status_code == 200
    await _expire_lease(job_id)

    sm = get_sessionmaker()
    async with sm() as s:
        recovered = await recover_expired_leases(s, utcnow())
        await s.commit()
    assert any(r["action"] == "requeued" and r["rescheduled"] for r in recovered)

    # Reservation was freed and a fresh one taken on reschedule (still 1 active).
    got = (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust))).json()
    assert got["status"] == "reserved"
    assert got["attempts_made"] == 1

    # Attempt 2: claim again, expire again -> retries exhausted -> TIMED_OUT.
    assert (await claim(client, m["machine_id"], m["secret"])).status_code == 200
    await _expire_lease(job_id)
    async with sm() as s:
        recovered2 = await recover_expired_leases(s, utcnow())
        await s.commit()
    assert any(r["action"] == "timed_out" for r in recovered2)

    got2 = (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust))).json()
    assert got2["status"] == "timed_out"
    # Capacity freed (no leaked reservation).
    assert await _active_slots(m["machine_id"]) == 0


# ===========================================================================
# customer isolation
# ===========================================================================
async def test_customer_b_cannot_see_a_job(client):
    await seed_echo_model()
    await create_listable_machine(client, "p-is@x.com", hostname="is")
    cust_a = await _customer(client, "c-is-a@x.com")
    cust_b = await _customer(client, "c-is-b@x.com")
    job_id = (await submit_job(client, cust_a)).json()["job_id"]

    assert (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust_b))).status_code == 404
    assert (
        await client.get(f"/v1/jobs/{job_id}/events", headers=auth_headers(cust_b))
    ).status_code == 404
    assert (
        await client.post(f"/v1/jobs/{job_id}/cancel", json={}, headers=auth_headers(cust_b))
    ).status_code == 404
    # A can see it.
    assert (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust_a))).status_code == 200


# ===========================================================================
# Audit fix H6 — a RESERVED job that is never claimed and whose reservation
# expired is recovered (not stuck forever), and its hold is released.
# ===========================================================================
async def test_stuck_reserved_job_is_recovered(client):
    from app.models.provider import ProviderMachine
    from app.services.leases import run_recovery_cycle

    await seed_echo_model()
    m = await create_listable_machine(client, "p-stuck@x.com", hostname="stuck")
    cust = await _customer(client, "c-stuck@x.com")
    sub = await submit_job(client, cust, machine_id=m["machine_id"])
    assert sub.json()["status"] == "reserved"
    job_id = uuid.UUID(sub.json()["job_id"])

    # The hold is outstanding while reserved.
    acct0 = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    assert acct0["reserved"] != "0.00"

    # Expire the reservation and take the machine offline so a reschedule fails.
    sm = get_sessionmaker()
    async with sm() as s:
        job = await s.get(Job, job_id)
        res = await s.get(ResourceReservation, job.reservation_id)
        res.expires_at = utcnow() - dt.timedelta(seconds=10)
        machine = await s.get(ProviderMachine, uuid.UUID(m["machine_id"]))
        machine.availability = "offline"
        await s.commit()

    # One recovery cycle must un-stick the job.
    async with sm() as s:
        await run_recovery_cycle(s)
        await s.commit()

    got = (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust))).json()
    assert got["status"] == "failed"
    assert got["error_code"] == "no_provider"

    # The hold is returned — no credit leak.
    acct1 = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    assert acct1["reserved"] == "0.00"


# ===========================================================================
# Workstream B — Hardware Execution & Metered Billing
# ===========================================================================
async def test_hardware_job_lifecycle_pinned_digest_and_metered_billing(client):
    """Workstream B: Hardware model job execution flow.
    1. Customer submits a text-generation job for tinyllama-1.1b-chat-v1.
    2. Scheduler matches provider and reserves capacity; execution_mode is 'hardware'.
    3. Provider claims job; response carries pinned image_digest and execution_mode 'hardware'.
    4. Provider completes execution and submits result with actual duration.
    5. Job completes with valid verification and is settled based on actual runtime.
    """
    from tests.helpers import seed_hardware_model

    await seed_hardware_model()
    m = await create_listable_machine(client, "p-hw@x.com", hostname="gpu-node-01")
    cust = await _customer(client, "c-hw@x.com")

    # 1. Submit hardware job
    sub = await submit_job(
        client,
        cust,
        model="tinyllama-1.1b-chat-v1",
        input={"prompt": "Explain distributed systems in one sentence."},
        estimated_minutes=10,
        max_cost="10.00",
    )
    assert sub.status_code == 201, sub.text
    job_data = sub.json()
    job_id = job_data["job_id"]
    assert job_data["status"] == "reserved"
    assert job_data["execution_mode"] == "hardware"

    # 2. Agent claims job
    claim_resp = await claim(client, m["machine_id"], m["secret"])
    assert claim_resp.status_code == 200, claim_resp.text
    claim_data = claim_resp.json()
    assert claim_data["job_id"] == job_id
    assert claim_data["execution_mode"] == "hardware"
    assert claim_data["image_digest"] is not None
    assert "@sha256:" in claim_data["image_digest"]

    # 3. Agent uploads output artifact
    token = claim_data["artifact_upload"]["token"]
    output_body = {
        "text": "A distributed system is a collection of autonomous computing entities that communicate via networks.",
        "model": "tinyllama-1.1b-chat-v1",
        "execution_mode": "hardware",
        "simulated": False,
        "tokens": 16,
    }
    up = await client.put(
        f"/v1/artifacts/upload/{token}",
        content=json.dumps(output_body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert up.status_code == 201
    artifact_id = up.json()["artifact_id"]

    # 4. Agent submits result with actual duration (5.2 seconds)
    result = await post_result(
        client,
        m["machine_id"],
        m["secret"],
        job_id,
        {
            "status": "success",
            "execution_mode": "hardware",
            "output": output_body,
            "artifacts": [{"artifact_id": artifact_id, "kind": "output"}],
            "metrics": {
                "duration_seconds": 5.2,
                "gpu_util_pct": 82.0,
                "max_memory_mb": 2100,
            },
        },
    )
    assert result.status_code == 200
    assert result.json()["status"] == "completed"

    # 5. Customer verifies job completion and honest hardware status
    got = (await client.get(f"/v1/jobs/{job_id}", headers=auth_headers(cust))).json()
    assert got["status"] == "completed"
    assert got["execution_mode"] == "hardware"
    assert got["verification"]["valid"] is True
    assert got["metrics"]["duration_seconds"] == 5.2
    assert got["metrics"]["gpu_util_pct"] == 82.0
    assert got["final_cost"] is not None



async def test_job_events_serialize_a_real_history(client):
    """GET /v1/jobs/{id}/events must survive a NON-EMPTY event list.

    Every job that actually runs accumulates events, so this serializer runs
    constantly in production -- but the suite only ever asserted the EMPTY case
    (an invalid transition appends nothing). An empty list never executes the
    per-row body, which is exactly how a wrong attribute name in the admin jobs
    serializer shipped as a permanent 500. This exercises the rows.
    """
    await seed_echo_model()
    await create_listable_machine(client, "p-ev@x.com", hostname="ev")
    cust = await _customer(client, "c-ev@x.com")
    job_id = (await submit_job(client, cust)).json()["job_id"]

    resp = await client.get(f"/v1/jobs/{job_id}/events", headers=auth_headers(cust))
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) >= 1, "a submitted job should already have a state history"

    for e in events:
        for field in ("seq", "to_state", "actor", "created_at"):
            assert field in e, f"event is missing {field}: {e}"

    # seq is a monotonic ordering, and the walk starts from no prior state.
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs), f"events are not ordered by seq: {seqs}"
    assert events[0]["from_state"] in (None, "created"), events[0]
    # The job really did progress past creation.
    assert any(e["to_state"] for e in events)
