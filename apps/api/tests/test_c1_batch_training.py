"""Phase C1 — batch submission, datasets and training-checkpoint plumbing.

These tests exercise the real pipeline: a batch creates real child jobs through
``POST /v1/jobs``, real quotes are discounted, real bytes move through the
object store behind signed grants. Nothing here asserts that training actually
happened — the platform stores *runner-reported* checkpoints and the simulated
runtime stays stamped ``execution_mode="simulated"``.
"""

from __future__ import annotations

import decimal
import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.main import app
from app.models.jobs import Job
from app.models.marketplace import Quote, ResourceReservation
from app.models.workloads import Batch, Dataset, TrainingCheckpoint
from app.db.session import get_sessionmaker
from app.services import storage
from tests.helpers import auth_headers, register_verify_login
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import seed_echo_model

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _wire_c1_routers():
    """Mount the Phase C1 routers.

    main.py is owned by the integrator; until it wires these in, the tests mount
    them onto the same app object so the suite exercises the real ASGI stack.
    """
    paths = {getattr(r, "path", "") for r in app.routes}
    if "/v1/batches" not in paths:
        from app.api.routers import batch as batch_router
        from app.api.routers import datasets as datasets_router

        app.include_router(batch_router.router)
        app.include_router(datasets_router.router)
        app.include_router(datasets_router.training_router)
    yield


@pytest.fixture(autouse=True)
def _object_store_tmp(tmp_path):
    """Point object storage at a temp dir (datasets/checkpoints write bytes)."""
    old_dir = settings.OBJECT_STORAGE_LOCAL_DIR
    old_max_ds = settings.DATASET_MAX_BYTES
    old_max_art = settings.ARTIFACT_MAX_BYTES
    settings.OBJECT_STORAGE_LOCAL_DIR = str(tmp_path / "objects")
    storage.reset_store()
    yield
    settings.OBJECT_STORAGE_LOCAL_DIR = old_dir
    settings.DATASET_MAX_BYTES = old_max_ds
    settings.ARTIFACT_MAX_BYTES = old_max_art
    storage.reset_store()


async def _customer(client, email: str) -> str:
    tokens = await register_verify_login(client, email)
    return tokens["access_token"]


def _item(prompt: str = "hello", **overrides) -> dict:
    body = {
        "workload_type": "text-generation",
        "model": "echo-sim-v1",
        "input": {"prompt": prompt},
        "gpu_count": 1,
        "estimated_minutes": 60,
        "max_runtime_seconds": 300,
    }
    body.update(overrides)
    return body


async def _post_batch(client, token, items, key: str | None = None):
    headers = auth_headers(token)
    if key:
        headers["Idempotency-Key"] = key
    return await client.post("/v1/batches", json={"items": items}, headers=headers)


async def _quote_for_job(job_id: uuid.UUID) -> Quote | None:
    sm = get_sessionmaker()
    async with sm() as s:
        job = await s.get(Job, job_id)
        if job is None or job.reservation_id is None:
            return None
        reservation = await s.get(ResourceReservation, job.reservation_id)
        return await s.get(Quote, reservation.quote_id)


# ===========================================================================
# Batch
# ===========================================================================
async def test_batch_creates_child_jobs_with_accurate_counts(client):
    await seed_echo_model()
    await create_listable_machine(client, "p-c1a@x.com", hostname="c1a", max_concurrent_jobs=3)
    cust = await _customer(client, "c-c1a@x.com")

    r = await _post_batch(client, cust, [_item("a"), _item("b"), _item("c")])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["total_items"] == 3
    assert body["submitted_items"] == 3
    assert body["pending_items"] == 3  # reserved jobs are not terminal
    assert body["status"] == "running"
    assert len({i["job_id"] for i in body["items"]}) == 3
    assert all(i["status"] == "reserved" for i in body["items"])

    # The children are real jobs on the normal pipeline, tagged with batch_id.
    jobs = await client.get("/v1/jobs", headers=auth_headers(cust))
    assert len(jobs.json()) == 3
    sm = get_sessionmaker()
    async with sm() as s:
        rows = (
            await s.execute(select(Job).where(Job.batch_id == uuid.UUID(body["batch_id"])))
        ).scalars().all()
        assert len(rows) == 3

    status_resp = await client.get(f"/v1/batches/{body['batch_id']}", headers=auth_headers(cust))
    assert status_resp.status_code == 200
    assert status_resp.json()["submitted_items"] == 3


async def test_batch_submit_is_idempotent(client):
    await seed_echo_model()
    await create_listable_machine(client, "p-c1b@x.com", hostname="c1b", max_concurrent_jobs=3)
    cust = await _customer(client, "c-c1b@x.com")

    r1 = await _post_batch(client, cust, [_item("a"), _item("b")], key="batch-key-1")
    r2 = await _post_batch(client, cust, [_item("a"), _item("b")], key="batch-key-1")
    assert r1.status_code == 201 and r2.status_code == 201, r2.text
    assert r1.json()["batch_id"] == r2.json()["batch_id"]

    # No duplicate children / holds.
    jobs = await client.get("/v1/jobs", headers=auth_headers(cust))
    assert len(jobs.json()) == 2
    sm = get_sessionmaker()
    async with sm() as s:
        batches = (await s.execute(select(Batch))).scalars().all()
        assert len(batches) == 1


async def test_batch_discount_applied_in_quote(client):
    await seed_echo_model()
    await create_listable_machine(
        client, "p-c1c@x.com", hostname="c1c", price="1.50", max_concurrent_jobs=2
    )
    cust = await _customer(client, "c-c1c@x.com")

    r = await _post_batch(client, cust, [_item("a")])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["discount_pct"].startswith("10")

    job_id = uuid.UUID(body["items"][0]["job_id"])
    quote = await _quote_for_job(job_id)
    assert quote is not None
    # 1.50/h discounted by 10% -> the hourly SNAPSHOT itself is discounted, so
    # settlement (which meters against the snapshot) charges the discount too.
    assert decimal.Decimal(quote.price_per_hour_snapshot) == decimal.Decimal("1.35")
    assert decimal.Decimal(quote.total) == decimal.Decimal("1.49")  # 1.35 + 0.14 fee

    q = body["quote"]
    assert q["priced_items"] == 1
    assert q["undiscounted_total"] == "1.65"  # 1.50 + 0.15 fee
    assert q["discounted_total"] == "1.49"
    assert decimal.Decimal(q["discount_amount"]) > 0


async def test_batch_cancel_stops_only_remaining_and_partial_is_never_completed(client):
    await seed_echo_model()
    await create_listable_machine(client, "p-c1d@x.com", hostname="c1d", max_concurrent_jobs=3)
    cust = await _customer(client, "c-c1d@x.com")

    # Item 1 fails validation at submit (terminal immediately); 2 and 3 succeed.
    bad = _item(input={"not_a_prompt": 1})
    r = await _post_batch(client, cust, [bad, _item("b"), _item("c")])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["submitted_items"] == 3
    assert body["failed_items"] == 1

    cancel = await client.post(
        f"/v1/batches/{body['batch_id']}/cancel", headers=auth_headers(cust)
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["cancelled"] == 2
    assert cancel.json()["skipped_terminal"] == 1

    after = (await client.get(f"/v1/batches/{body['batch_id']}", headers=auth_headers(cust))).json()
    states = sorted(i["status"] for i in after["items"])
    assert states == ["cancelled", "cancelled", "failed"]
    # A batch with a failed child is NEVER reported complete.
    assert after["status"] == "failed"
    assert after["completed_items"] == 0


async def test_batch_non_owner_gets_404(client):
    await seed_echo_model()
    await create_listable_machine(client, "p-c1e@x.com", hostname="c1e", max_concurrent_jobs=2)
    owner = await _customer(client, "c-c1e-own@x.com")
    other = await _customer(client, "c-c1e-oth@x.com")

    r = await _post_batch(client, owner, [_item("a")])
    batch_id = r.json()["batch_id"]
    assert (await client.get(f"/v1/batches/{batch_id}", headers=auth_headers(other))).status_code == 404
    assert (
        await client.post(f"/v1/batches/{batch_id}/cancel", headers=auth_headers(other))
    ).status_code == 404
    assert (await client.get("/v1/batches", headers=auth_headers(other))).json() == []


async def test_batch_over_max_items_rejected(client):
    await seed_echo_model()
    cust = await _customer(client, "c-c1f@x.com")
    old = settings.BATCH_MAX_ITEMS
    settings.BATCH_MAX_ITEMS = 2
    try:
        r = await _post_batch(client, cust, [_item("a"), _item("b"), _item("c")])
    finally:
        settings.BATCH_MAX_ITEMS = old
    assert r.status_code == 422
    assert r.json()["detail"] == "batch_too_large"
    sm = get_sessionmaker()
    async with sm() as s:
        assert (await s.execute(select(Job))).scalars().all() == []


# ===========================================================================
# Datasets
# ===========================================================================
async def _create_dataset(client, token, *, size=16, content_type="text/csv", filename="d.csv"):
    return await client.post(
        "/v1/datasets",
        json={"filename": filename, "content_type": content_type, "size_bytes": size},
        headers=auth_headers(token),
    )


async def test_dataset_signed_upload_download_roundtrip(client):
    cust = await _customer(client, "c-ds1@x.com")
    payload = b"a,b\n1,2\n"
    grant = await _create_dataset(client, cust, size=len(payload))
    assert grant.status_code == 201, grant.text
    g = grant.json()
    assert g["max_bytes"] == settings.DATASET_MAX_BYTES

    up = await client.put(g["url"], content=payload, headers={"Content-Type": "text/csv"})
    assert up.status_code == 200, up.text
    assert up.json()["status"] == "uploaded"
    import hashlib

    assert up.json()["sha256"] == hashlib.sha256(payload).hexdigest()
    assert up.json()["size_bytes"] == len(payload)

    dl = await client.get(f"/v1/datasets/{g['dataset_id']}/download", headers=auth_headers(cust))
    assert dl.status_code == 200, dl.text
    blob = await client.get(dl.json()["url"])
    assert blob.status_code == 200
    assert blob.content == payload
    assert blob.headers["x-content-type-options"] == "nosniff"

    # A forged/garbage grant is refused.
    assert (await client.get("/v1/datasets/download/not-a-token")).status_code == 401


async def test_dataset_oversize_and_bad_content_type_rejected(client):
    cust = await _customer(client, "c-ds2@x.com")

    bad_ct = await _create_dataset(client, cust, content_type="text/html")
    assert bad_ct.status_code == 415
    assert bad_ct.json()["detail"] == "content_type_not_allowed"

    settings.DATASET_MAX_BYTES = 8
    too_big_declared = await _create_dataset(client, cust, size=1024)
    assert too_big_declared.status_code == 413
    assert too_big_declared.json()["detail"] == "dataset_too_large"

    # A truthful small declaration followed by oversized BYTES is also refused.
    grant = await _create_dataset(client, cust, size=4)
    assert grant.status_code == 201, grant.text
    up = await client.put(
        grant.json()["url"], content=b"x" * 64, headers={"Content-Type": "text/csv"}
    )
    assert up.status_code == 413
    sm = get_sessionmaker()
    async with sm() as s:
        ds_row = await s.get(Dataset, uuid.UUID(grant.json()["dataset_id"]))
        assert ds_row.status == "pending" and ds_row.sha256 == ""


async def test_dataset_non_owner_gets_404(client):
    owner = await _customer(client, "c-ds3-own@x.com")
    other = await _customer(client, "c-ds3-oth@x.com")
    g = (await _create_dataset(client, owner, size=4)).json()
    await client.put(g["url"], content=b"a,b\n", headers={"Content-Type": "text/csv"})

    assert (
        await client.get(f"/v1/datasets/{g['dataset_id']}", headers=auth_headers(other))
    ).status_code == 404
    assert (
        await client.get(f"/v1/datasets/{g['dataset_id']}/download", headers=auth_headers(other))
    ).status_code == 404
    assert (await client.get("/v1/datasets", headers=auth_headers(other))).json() == []


# ===========================================================================
# Training: checkpoints + resume
# ===========================================================================
async def _job_for_training(client, token, prompt="train"):
    r = await client.post("/v1/jobs", json=_item(prompt), headers=auth_headers(token))
    assert r.status_code == 201, r.text
    return r.json()["job_id"]


async def _uploaded_dataset(client, token) -> str:
    g = (await _create_dataset(client, token, size=8)).json()
    up = await client.put(g["url"], content=b"a,b\n1,2\n", headers={"Content-Type": "text/csv"})
    assert up.status_code == 200, up.text
    return g["dataset_id"]


async def test_training_checkpoint_recorded_and_resume_reference(client):
    await seed_echo_model()
    await create_listable_machine(client, "p-tr1@x.com", hostname="tr1", max_concurrent_jobs=3)
    cust = await _customer(client, "c-tr1@x.com")

    dataset_id = await _uploaded_dataset(client, cust)
    job_id = await _job_for_training(client, cust)
    run = await client.post(
        "/v1/training/runs",
        json={"job_id": job_id, "dataset_id": dataset_id, "hyperparameters": {"lr": "3e-4"}},
        headers=auth_headers(cust),
    )
    assert run.status_code == 201, run.text
    # Honest stamping: the simulated runtime is not real training.
    assert run.json()["execution_mode"] == "simulated"
    assert run.json()["dataset_id"] == dataset_id

    # The runner reports a checkpoint behind a signed grant.
    grant = await client.post(
        f"/v1/training/runs/{job_id}/checkpoint-grants", headers=auth_headers(cust)
    )
    assert grant.status_code == 201, grant.text
    blob = b"CKPT-BYTES"
    up = await client.put(
        grant.json()["url"],
        content=blob,
        headers={"X-Checkpoint-Step": "1", "X-Checkpoint-Metrics": '{"loss": 0.42}'},
    )
    assert up.status_code == 201, up.text
    checkpoint_id = up.json()["checkpoint_id"]
    assert up.json()["step"] == 1
    assert up.json()["metrics"]["reported"]["loss"] == 0.42
    assert up.json()["metrics"]["source"] == "runner_reported"
    assert up.json()["metrics"]["execution_mode"] == "simulated"

    # Idempotent per (job, step): re-reporting the same step does not duplicate.
    again = await client.put(
        grant.json()["url"], content=blob, headers={"X-Checkpoint-Step": "1"}
    )
    assert again.status_code == 201
    assert again.json()["checkpoint_id"] == checkpoint_id
    sm = get_sessionmaker()
    async with sm() as s:
        rows = (await s.execute(select(TrainingCheckpoint))).scalars().all()
        assert len(rows) == 1

    listed = await client.get(
        f"/v1/training/runs/{job_id}/checkpoints", headers=auth_headers(cust)
    )
    assert listed.status_code == 200 and len(listed.json()) == 1

    # Owner can download the checkpoint bytes via a short-lived grant.
    dl = await client.get(
        f"/v1/training/checkpoints/{checkpoint_id}/download", headers=auth_headers(cust)
    )
    assert dl.status_code == 200, dl.text
    got = await client.get(dl.json()["url"])
    assert got.status_code == 200 and got.content == blob

    # Resume: a NEW job references the checkpoint.
    job2 = await _job_for_training(client, cust, prompt="resume")
    run2 = await client.post(
        "/v1/training/runs",
        json={
            "job_id": job2,
            "dataset_id": dataset_id,
            "resume_from_checkpoint_id": checkpoint_id,
        },
        headers=auth_headers(cust),
    )
    assert run2.status_code == 201, run2.text
    resume = run2.json()["resume_from"]
    assert resume["checkpoint_id"] == checkpoint_id
    assert resume["step"] == 1
    assert resume["object_key"]
    # The spec reaches the runner through the job input the agent claims.
    async with sm() as s:
        job_row = await s.get(Job, uuid.UUID(job2))
        assert job_row.input["training"]["dataset_id"] == dataset_id
        assert job_row.input["training"]["resume_from"]["step"] == 1


async def test_training_checkpoint_and_run_non_owner_404(client):
    await seed_echo_model()
    await create_listable_machine(client, "p-tr2@x.com", hostname="tr2", max_concurrent_jobs=3)
    owner = await _customer(client, "c-tr2-own@x.com")
    other = await _customer(client, "c-tr2-oth@x.com")

    dataset_id = await _uploaded_dataset(client, owner)
    job_id = await _job_for_training(client, owner)
    await client.post(
        "/v1/training/runs",
        json={"job_id": job_id, "dataset_id": dataset_id},
        headers=auth_headers(owner),
    )
    grant = await client.post(
        f"/v1/training/runs/{job_id}/checkpoint-grants", headers=auth_headers(owner)
    )
    cp = await client.put(
        grant.json()["url"], content=b"x", headers={"X-Checkpoint-Step": "0"}
    )
    checkpoint_id = cp.json()["checkpoint_id"]

    assert (
        await client.get(f"/v1/training/runs/{job_id}", headers=auth_headers(other))
    ).status_code == 404
    assert (
        await client.get(f"/v1/training/runs/{job_id}/checkpoints", headers=auth_headers(other))
    ).status_code == 404
    assert (
        await client.post(
            f"/v1/training/runs/{job_id}/checkpoint-grants", headers=auth_headers(other)
        )
    ).status_code == 404
    assert (
        await client.get(
            f"/v1/training/checkpoints/{checkpoint_id}/download", headers=auth_headers(other)
        )
    ).status_code == 404
    # A non-owner cannot attach their dataset to someone else's job either.
    other_ds = await _uploaded_dataset(client, other)
    assert (
        await client.post(
            "/v1/training/runs",
            json={"job_id": job_id, "dataset_id": other_ds},
            headers=auth_headers(other),
        )
    ).status_code == 404
