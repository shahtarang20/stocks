"""Regression tests for bugs found by the full-feature QA coverage run (launch eve).

Each test fails without its fix (see the docstring for the original symptom)."""

from __future__ import annotations

import pytest

from tests.helpers import auth_headers
from tests.test_provider_phase2 import become_provider, enroll_machine, mint_token

pytestmark = pytest.mark.asyncio


async def _machine(client, email="qa-price@example.com"):
    access = await become_provider(client, email)
    tok = await mint_token(client, access)
    m = await enroll_machine(client, tok["enrollment_token"])
    return auth_headers(access), m["machine_id"]


@pytest.mark.parametrize("bad", ["-1", "0", "0.001", "1e30", "99999999999999", "NaN", "Infinity", "abc"])
async def test_patch_machine_rejects_bad_asking_price(client, bad):
    """PATCH /provider/machines/{id} used to store -1, 0, 0.00 (from 0.001) and 1e30 as the asking price."""
    headers, mid = await _machine(client)
    r = await client.patch(f"/provider/machines/{mid}", json={"asking_price_per_hour": bad}, headers=headers)
    assert r.status_code == 422, (bad, r.text)


@pytest.mark.parametrize("bad", ["1e30", "99999999999999", "0.001", "-1", "0"])
async def test_put_pricing_rejects_bad_asking_price(client, bad):
    """PUT /v1/provider/machines/{id}/pricing raised decimal.InvalidOperation (HTTP 500) for 1e30."""
    headers, mid = await _machine(client, "qa-price2@example.com")
    r = await client.put(f"/v1/provider/machines/{mid}/pricing", json={"asking_price_per_hour": bad}, headers=headers)
    assert r.status_code == 422, (bad, r.text)


async def test_patch_machine_accepts_and_quantizes_good_price(client):
    headers, mid = await _machine(client, "qa-price3@example.com")
    r = await client.patch(f"/provider/machines/{mid}", json={"asking_price_per_hour": "1.239"}, headers=headers)
    assert r.status_code == 200 and r.json()["asking_price_per_hour"] == "1.24"


async def test_quote_and_job_reject_overflowing_numbers(client):
    """Absurd numbers overflowed the DB int column / Decimal arithmetic and returned HTTP 500 (or reset the connection)."""
    headers, mid = await _machine(client, "qa-ovf@example.com")
    from tests.helpers import register_verify_login

    cust = auth_headers((await register_verify_login(client, "qa-ovf-c@example.com"))["access_token"])
    base_job = {"workload_type": "text-generation", "model": "echo-sim-v1", "machine_id": mid, "max_cost": "5.00"}
    for body in (
        {"machine_id": mid, "workload_type": "text-generation", "estimated_minutes": 5, "max_authorized_spend": "1e400"},
        {"machine_id": mid, "workload_type": "text-generation", "estimated_minutes": 5, "max_authorized_spend": "1e30"},
        {"machine_id": mid, "workload_type": "text-generation", "estimated_minutes": 2**63},
        {"machine_id": mid, "workload_type": "text-generation", "estimated_minutes": 5, "gpu_count": 10**6},
    ):
        r = await client.post("/v1/quotes", json=body, headers=cust)
        assert r.status_code == 422, (body, r.status_code, r.text)
    for extra in ({"estimated_minutes": 2**63}, {"estimated_minutes": 10**12}, {"max_runtime_seconds": 2**63},
                  {"priority": 2**63}, {"priority": 10**12}):
        r = await client.post("/v1/jobs", json={**base_job, **extra}, headers=cust)
        assert r.status_code == 422, (extra, r.status_code, r.text)


async def test_kit_plan_outputs_are_relative_and_accepted_by_agent(client):
    """Kit manifests name outputs '/out/<path>'; the claim plan carried them verbatim and the agent refused them
    ('invalid_v2_plan: bad output path'), so EVERY Kit job failed at the agent."""
    import uuid
    from types import SimpleNamespace

    from app.api.routers.jobs_v2 import build_v2_plan
    from app.db.session import get_sessionmaker

    kit = SimpleNamespace(slug="k", version="1", manifest={"outputs": [
        {"name": "frames", "path": "/out/frames"}, {"name": "meta", "path": "/out/meta.json"}]})
    async with get_sessionmaker()() as db:
        plan = await build_v2_plan(db, uuid.uuid4(), SimpleNamespace(inputs=[], outputs=[]), kit)
    assert [o["path"] for o in plan["outputs"]] == ["frames", "meta.json"]
    rv = pytest.importorskip("computecoin_agent.runtime_v2")
    parsed = rv.parse_plan({"kit": {"slug": "k"}, "outputs": plan["outputs"]}, min_interval=60)
    assert [o["path"] for o in parsed.outputs] == ["frames", "meta.json"]


async def test_agent_v2_output_upload_accepts_platform_namespaced_name(client, monkeypatch):
    """POST /v1/agents/v2/jobs/{id}/uploads names the file 'out/<job>/<output>' but data_plane.clean_name refused '/'
    (422 invalid_name), so no Kit output (or checkpoint) could ever be uploaded. Customers still cannot use '/'."""
    import hashlib
    import json
    import uuid

    from app.core.config import settings
    from app.db.session import get_sessionmaker
    from app.models.jobs import Job
    from app.services import job_fsm as fsm
    from app.services.data_plane_store import set_data_store
    from tests.helpers import get_user_id, register_verify_login
    from tests.test_provider_phase2 import heartbeat_headers
    from tests.test_u2_data_plane import FakeStore

    store = FakeStore()
    set_data_store(store)
    for k, v in dict(DATA_PLANE_ENABLED=True, DATA_PLANE_ENDPOINT="https://s3.example", DATA_PLANE_BUCKET="b",
                     DATA_PLANE_ACCESS_KEY="k", DATA_PLANE_SECRET_KEY_ID="s").items():
        monkeypatch.setattr(settings, k, v)
    try:
        access = await become_provider(client, "qa-v2up-p@example.com")
        tok = await mint_token(client, access)
        m = await enroll_machine(client, tok["enrollment_token"])
        await register_verify_login(client, "qa-v2up-c@example.com")
        cid = await get_user_id("qa-v2up-c@example.com")
        job_id = uuid.uuid4()
        async with get_sessionmaker()() as s:
            s.add(Job(
                id=job_id, customer_id=cid, workload_type="kit", model="k@1", input={}, status=fsm.RUNNING, gpu_count=1,
                estimated_minutes=2, max_runtime_seconds=600, execution_mode="hardware", isolation_tier="container",
                machine_id=uuid.UUID(m["machine_id"]),
                data_policy={"v2_plan": {"kit": {"slug": "k", "version": "1"}, "inputs": [], "assets": [],
                                         "outputs": [{"name": "frames", "path": "frames", "required": True}], "egress": []}},
            ))
            await s.commit()
        data = b"x" * 1000
        body = {"purpose": "output", "name": "frames", "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
        headers, raw = heartbeat_headers(m["machine_id"], m["machine_secret"], body)
        raw = json.dumps(body).encode()
        headers, _ = heartbeat_headers(m["machine_id"], m["machine_secret"], body)
        r = await client.post(f"/v1/agents/v2/jobs/{job_id}/uploads", content=raw, headers=headers)
        assert r.status_code == 200, r.text
        # a customer cannot create a file whose name looks like a platform namespace
        h = auth_headers((await __import__("tests.helpers", fromlist=["login"]).login(client, "qa-v2up-c@example.com"))["access_token"])
        r = await client.post("/v1/data/uploads", json={"name": f"out/{job_id}/frames", "sha256": "a" * 64, "size_bytes": 5},
                              headers={**h, "Idempotency-Key": "k1"})
        assert r.status_code == 422 and r.json()["detail"] == "invalid_name", r.text
    finally:
        set_data_store(None)


async def test_result_receipt_accepts_v2_output_descriptor():
    """The agent's v2 receipt lists {kind, name, sha256, size, content_type, file_id} (no artifact_id); the schema required
    artifact_id, so POST /v1/agents/jobs/{id}/result answered 400 invalid_body for every Kit job with outputs."""
    import uuid

    from app.schemas.jobs import JobResultRequest

    fid = str(uuid.uuid4())
    r = JobResultRequest.model_validate({"status": "success", "artifacts": [
        {"kind": "output", "name": "frames", "sha256": "a" * 64, "size": 5, "content_type": "application/x-tar", "file_id": fid}]})
    assert str(r.artifacts[0].file_id) == fid and r.artifacts[0].artifact_id is None
    v1 = JobResultRequest.model_validate({"status": "success", "artifacts": [{"artifact_id": fid, "kind": "output"}]})
    assert str(v1.artifacts[0].artifact_id) == fid


def test_pipeline_step_without_inputs_key_gets_edge_placeholders():
    """A step spec that omits "inputs" (all inputs come from edges) was not given the edge placeholders, so pipeline
    creation failed with 'input ... is required' for the most natural way to write a chain."""
    from app.services import pipelines as svc

    edges = [{"from_step": "a", "output": "out", "to_step": "b", "input": "inp"}]
    omitted = svc._placeholder_spec({"id": "b", "spec": {"version": 2, "kit": "x@1.0.0"}}, edges)
    explicit = svc._placeholder_spec({"id": "b", "spec": {"version": 2, "kit": "x@1.0.0", "inputs": []}}, edges)
    assert [i["name"] for i in omitted["inputs"]] == ["inp"] == [i["name"] for i in explicit["inputs"]]
    # a malformed inputs value is left for the spec validator to reject
    bad = svc._placeholder_spec({"id": "b", "spec": {"version": 2, "kit": "x@1.0.0", "inputs": "nope"}}, edges)
    assert bad["inputs"] == "nope"


async def test_session_create_rejects_overflowing_price(client):
    """POST /v1/sessions with max_price_per_gpu_hour='1e400' raised decimal.InvalidOperation (HTTP 500)."""
    from tests.helpers import register_verify_login

    h = auth_headers((await register_verify_login(client, "qa-sess-ovf@example.com"))["access_token"])
    body = {"gpu_count": 1, "max_price_per_gpu_hour": "1e400", "client_pubkey": "ab" * 32, "image_digest": "r/x@sha256:" + "a" * 64}
    for bad in ("1e400", "1e30"):
        r = await client.post("/v1/sessions", json={**body, "max_price_per_gpu_hour": bad}, headers=h)
        assert r.status_code == 422, (bad, r.status_code, r.text)


def test_byoc_result_is_not_rejected_as_unknown_workload():
    """BYOC jobs have workload_type 'byoc'; verify_output only knew 'kit', so every BYOC job result failed
    output_verification_failed / unknown_workload (the customer was never charged and the provider never paid)."""
    from app.services import verification as v

    assert v.verify_output("byoc", {}, {})["valid"] is True
    assert v.verify_output("byoc", {}, None)["valid"] is False  # still must be an object
    assert v.verify_output("nope", {}, {})["reason"] == "unknown_workload"


async def test_non_streaming_chat_refuses_private_tier(client):
    """POST /v1/chat/completions (stream=false) with isolation_tier=sealed ignored the tier and answered as a plain
    container job: the privacy request was silently downgraded (fail-open)."""
    from tests.helpers import register_verify_login

    h = auth_headers((await register_verify_login(client, "qa-chat-sealed@example.com"))["access_token"])
    body = {"model": "echo-sim-v1", "messages": [{"role": "user", "content": "secret"}]}
    for tier in ("sealed", "sealed_pro", "confidential", "SEALED"):
        r = await client.post("/v1/chat/completions", json={**body, "isolation_tier": tier}, headers=h)
        assert r.status_code == 400, (tier, r.status_code, r.text)
        assert r.json()["detail"]["error"]["code"] == "private_tier_requires_streaming"
    # a non-private tier is not caught by the new guard (it proceeds to model resolution; no model is seeded here)
    other = await client.post("/v1/chat/completions", json={**body, "isolation_tier": "container"}, headers=h)
    assert other.json().get("detail", {}).get("error", {}).get("code") != "private_tier_requires_streaming"


async def test_public_consistency_route_and_settlement_record_reject_overflow(client):
    """GET /v1/privacy/transparency-log/consistency (public) and POST /v1/settlement/transactions (admin) returned 500 for
    values beyond the DB integer range."""
    from app.models.enums import Role
    from tests.helpers import grant_role, login, register_verify_login

    for q in ({"from_size": 1, "to_size": 2**63}, {"from_size": 2**63, "to_size": 2**63}, {"from_size": 1, "to_size": 10**30}):
        r = await client.get("/v1/privacy/transparency-log/consistency", params=q)
        assert r.status_code == 422, (q, r.status_code, r.text)
    await register_verify_login(client, "qa-settle-adm@example.com")
    await grant_role("qa-settle-adm@example.com", Role.administrator)
    h = auth_headers((await login(client, "qa-settle-adm@example.com"))["access_token"])
    base = {"chain_id": 31337, "tx_hash": "0x" + "a" * 64, "tx_type": "x", "sender_address": "0x" + "1" * 40}
    for extra in ({"chain_id": 2**63}, {"block_number": 2**63}, {"block_number": -1}):
        r = await client.post("/v1/settlement/transactions", json={**base, **extra}, headers=h)
        assert r.status_code == 422, (extra, r.status_code, r.text)
    r = await client.post("/v1/settlement/confirm", json={"job_id": "00000000-0000-0000-0000-000000000001",
                          "settle_tx_hash": "0x" + "a" * 64, "block_number": 2**63}, headers=h)
    assert r.status_code == 422, r.text


async def test_alert_acknowledge_is_owner_scoped(client):
    """Any signed-in user could acknowledge ANY telemetry alert by id (the list route was already scoped to the owning
    provider / administrator)."""
    from app.db.session import get_sessionmaker
    from app.models.enums import Role
    from app.models.telemetry import TelemetryAlert
    from tests.helpers import grant_role, login, register_verify_login

    owner_h, mid = await _machine(client, "qa-alert-owner@example.com")
    other_h, _ = await _machine(client, "qa-alert-other@example.com")
    cust = auth_headers((await register_verify_login(client, "qa-alert-cust@example.com"))["access_token"])
    await register_verify_login(client, "qa-alert-adm@example.com")
    await grant_role("qa-alert-adm@example.com", Role.administrator)
    adm = auth_headers((await login(client, "qa-alert-adm@example.com"))["access_token"])
    import uuid as _uuid

    ids = []
    async with get_sessionmaker()() as s:
        for _ in range(3):
            a = TelemetryAlert(machine_id=_uuid.UUID(mid), alert_type="gpu_overheat", severity="warning", message="hot", status="active")
            s.add(a)
            await s.flush()
            ids.append(a.id)
        await s.commit()
    for h in (cust, other_h):
        r = await client.post(f"/v1/telemetry/alerts/{ids[0]}/acknowledge", headers=h)
        assert r.status_code == 404, r.text
    async with get_sessionmaker()() as s:
        assert (await s.get(TelemetryAlert, ids[0])).status == "active"  # untouched by the refused attempts
    assert (await client.post(f"/v1/telemetry/alerts/{ids[0]}/acknowledge", headers=owner_h)).status_code == 200
    assert (await client.post(f"/v1/telemetry/alerts/{ids[1]}/acknowledge", headers=adm)).status_code == 200
