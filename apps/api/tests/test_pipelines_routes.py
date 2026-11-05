"""U9 pipeline ROUTES through the ASGI client (written, NOT run).

Graph-level refusals need no Kit and no store. Where a run must be persisted, ``validate_pipeline`` is replaced by the
pure graph parse and ``advance_run`` by a no-op, so these tests check the HTTP contract (validation errors per field,
get, cancel, restart, idempotency, privacy refusal) and not job scheduling (covered in test_pipelines_u9.py).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.session import get_sessionmaker
from app.models.pipelines import (
    RUN_CANCELLED, RUN_FAILED, RUN_PENDING, RUN_RUNNING, STEP_CANCELLED, STEP_FAILED, STEP_PENDING, STEP_SUCCEEDED,
    PipelineRun, PipelineStep,
)
from app.services import data_plane as dp
from app.services import pipelines as svc
from tests.helpers import auth_headers, get_user_id, register_verify_login

pytestmark = pytest.mark.asyncio


def _step(sid, kind="job", **spec):
    return {"id": sid, "kind": kind, "spec": {"version": 2, "kit": f"{sid}@1.0.0", **spec}}


def _chain():
    return {"version": 1, "name": "chain", "steps": [_step("a"), _step("b"), _step("c")],
            "edges": [{"from": "a.out", "to": "b.inp"}, {"from": "b.out", "to": "c.inp"}]}


def _errs(resp):
    return {(e["field"], e["code"]) for e in resp.json()["detail"]["errors"]}


@pytest.fixture
def plane_on(monkeypatch):
    """The data plane check answers first; replace it so graph validation is reachable."""
    monkeypatch.setattr(dp, "require_enabled", lambda: object())


@pytest.fixture
def graph_only(monkeypatch, plane_on):
    async def validate(db, customer_id, raw, *, isolation_tier="container"):
        return svc.parse_graph(raw)

    async def advance(db, run):
        return {}

    monkeypatch.setattr(svc, "validate_pipeline", validate)
    monkeypatch.setattr(svc, "advance_run", advance)


async def _login(client, email):
    return auth_headers((await register_verify_login(client, email))["access_token"])


async def test_requires_auth_and_data_plane(client, monkeypatch):
    assert (await client.post("/v1/pipelines", json={"spec": _chain()})).status_code in (401, 403)
    h = await _login(client, "pl-auth@example.com")
    monkeypatch.setattr(settings, "DATA_PLANE_ENABLED", False)
    r = await client.post("/v1/pipelines", headers=h, json={"spec": _chain()})
    assert r.status_code == 404 and r.json()["detail"] == "data_plane_disabled"


async def test_create_validation_errors_per_field(client, plane_on):
    h = await _login(client, "pl-val@example.com")
    spec = _chain()
    spec["bogus"] = 1
    spec["version"] = 2
    spec["name"] = ""
    spec["steps"].append(_step("a"))  # duplicate id
    spec["steps"].append({"id": "BAD ID", "spec": {}})
    spec["edges"] = [{"from": "a.out", "to": "a.inp"}, {"from": "nope.x", "to": "b.y"}, {"from": "x", "to": "y"},
                     {"from": "a.o1", "to": "c.same"}, {"from": "b.o2", "to": "c.same"}]
    r = await client.post("/v1/pipelines", headers=h, json={"spec": spec})
    assert r.status_code == 422, r.text
    got = _errs(r)
    for expected in [("bogus", "unknown_field"), ("version", "unsupported_version"), ("name", "invalid_name"),
                     ("steps[3].id", "duplicate_id"), ("steps[4].id", "invalid_id"), ("edges[0]", "self_edge"),
                     ("edges[1].from", "unknown_step"), ("edges[2].from", "invalid_endpoint"),
                     ("edges[4].to", "input_fed_twice")]:
        assert expected in got, (expected, got)


async def test_empty_steps_and_unknown_body_field(client, plane_on):
    h = await _login(client, "pl-empty@example.com")
    r = await client.post("/v1/pipelines", headers=h, json={"spec": {"version": 1, "steps": []}})
    assert r.status_code == 422 and ("steps", "must_be_non_empty_list") in _errs(r)
    r = await client.post("/v1/pipelines", headers=h, json={"spec": _chain(), "surprise": True})
    assert r.status_code == 422  # extra="forbid" on the request body


async def test_max_steps_cap(client, plane_on, monkeypatch):
    monkeypatch.setattr(settings, "PIPELINE_MAX_STEPS", 2)
    h = await _login(client, "pl-cap@example.com")
    r = await client.post("/v1/pipelines", headers=h, json={"spec": {"version": 1, "steps": [_step("a"), _step("b"), _step("c")]}})
    assert r.status_code == 422 and ("steps", "too_many_steps") in _errs(r)


async def test_cycle_refused(client, plane_on):
    h = await _login(client, "pl-cycle@example.com")
    spec = _chain()
    spec["edges"].append({"from": "c.out", "to": "a.inp"})
    r = await client.post("/v1/pipelines", headers=h, json={"spec": spec})
    assert r.status_code == 422 and ("edges", "cycle") in _errs(r)
    async with get_sessionmaker()() as db:
        assert (await db.execute(select(PipelineRun))).first() is None  # nothing was stored


@pytest.mark.parametrize("tier", ["sealed", "sealed_pro", "confidential"])
async def test_privacy_tier_refused(client, plane_on, tier):
    h = await _login(client, f"pl-priv-{tier}@example.com")
    r = await client.post("/v1/pipelines", headers=h, json={"spec": _chain(), "isolation_tier": tier})
    assert r.status_code == 422 and r.json()["detail"] == "privacy_tier_not_supported_for_pipelines"
    r = await client.post("/v1/pipelines", headers=h, json={"spec": _chain(), "isolation_tier": "nonsense"})
    assert r.status_code == 422


async def test_create_get_and_other_user_cannot_read(client, graph_only):
    h = await _login(client, "pl-get@example.com")
    r = await client.post("/v1/pipelines", headers=h, json={"spec": _chain()})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "chain" and body["status"] == RUN_PENDING and body["restart_count"] == 0
    assert [s["id"] for s in body["steps"]] == ["a", "b", "c"]
    assert body["steps"][1]["needs"] == [{"from_step": "a", "output": "out", "input": "inp"}]
    assert all(s["status"] == STEP_PENDING for s in body["steps"])

    got = await client.get(f"/v1/pipelines/{body['id']}", headers=h)
    assert got.status_code == 200 and got.json()["id"] == body["id"]
    assert (await client.get(f"/v1/pipelines/{uuid.uuid4()}", headers=h)).status_code == 404
    other = await _login(client, "pl-get-other@example.com")
    assert (await client.get(f"/v1/pipelines/{body['id']}", headers=other)).status_code == 404


async def test_create_is_idempotent_by_key(client, graph_only):
    h = await _login(client, "pl-idem@example.com")
    one = await client.post("/v1/pipelines", headers={**h, "Idempotency-Key": "k-1"}, json={"spec": _chain()})
    two = await client.post("/v1/pipelines", headers={**h, "Idempotency-Key": "k-1"}, json={"spec": _chain()})
    assert one.status_code == 201 and two.status_code == 201
    assert one.json()["id"] == two.json()["id"]
    async with get_sessionmaker()() as db:
        assert len((await db.execute(select(PipelineRun))).scalars().all()) == 1


async def test_cancel_marks_waiting_steps_and_is_idempotent(client, graph_only):
    h = await _login(client, "pl-cancel@example.com")
    pid = (await client.post("/v1/pipelines", headers=h, json={"spec": _chain()})).json()["id"]
    r = await client.post(f"/v1/pipelines/{pid}/cancel", headers=h)
    assert r.status_code == 200 and r.json()["status"] == RUN_CANCELLED
    assert all(s["status"] == STEP_CANCELLED and s["error_code"] == "customer_cancel" for s in r.json()["steps"])
    again = await client.post(f"/v1/pipelines/{pid}/cancel", headers=h)
    assert again.status_code == 200 and again.json()["status"] == RUN_CANCELLED
    assert (await client.post(f"/v1/pipelines/{uuid.uuid4()}/cancel", headers=h)).status_code == 404
    # a cancelled run is not restartable
    assert (await client.post(f"/v1/pipelines/{pid}/restart", headers=h)).status_code == 409


async def _fail_run(pid: str, email: str):
    """Put a stored run into 'failed' with step a succeeded and step b failed (hand-built DB state)."""
    async with get_sessionmaker()() as db:
        run = await db.get(PipelineRun, uuid.UUID(pid))
        run.status, run.error_code = RUN_FAILED, "step_failed:b"
        steps = {s.step_key: s for s in (await db.execute(select(PipelineStep).where(PipelineStep.run_id == run.id))).scalars()}
        steps["a"].status, steps["a"].attempts, steps["a"].outputs = STEP_SUCCEEDED, 1, {}  # no outputs: nothing to re-check
        steps["b"].status, steps["b"].attempts, steps["b"].error_code = STEP_FAILED, 1, "oom"
        await db.commit()


async def test_restart_only_when_failed_and_repeat_is_refused(client, graph_only):
    h = await _login(client, "pl-restart@example.com")
    pid = (await client.post("/v1/pipelines", headers=h, json={"spec": _chain()})).json()["id"]
    not_failed = await client.post(f"/v1/pipelines/{pid}/restart", headers=h)
    assert not_failed.status_code == 409 and ("pipeline", "not_restartable") in _errs(not_failed)

    await _fail_run(pid, "pl-restart@example.com")
    ok = await client.post(f"/v1/pipelines/{pid}/restart", headers=h)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == RUN_RUNNING and body["restart_count"] == 1 and body["error_code"] is None
    assert body["restart"]["kept_steps"] == ["a"] and body["restart"]["restarted_steps"] == ["b", "c"]
    by_id = {s["id"]: s for s in body["steps"]}
    assert by_id["a"]["status"] == STEP_SUCCEEDED  # reused, never re-run (no second job, no double billing)
    assert by_id["b"]["status"] == STEP_PENDING and by_id["b"]["history"][0]["error_code"] == "oom"
    # Idempotent in effect: a second restart on the now-running run changes nothing.
    again = await client.post(f"/v1/pipelines/{pid}/restart", headers=h)
    assert again.status_code == 409
    assert (await client.get(f"/v1/pipelines/{pid}", headers=h)).json()["restart_count"] == 1


async def test_restart_limit_and_attempt_cap(client, graph_only, monkeypatch):
    h = await _login(client, "pl-restart-cap@example.com")
    pid = (await client.post("/v1/pipelines", headers=h, json={"spec": _chain()})).json()["id"]
    await _fail_run(pid, "pl-restart-cap@example.com")
    monkeypatch.setattr(settings, "PIPELINE_MAX_STEP_ATTEMPTS", 1)
    r = await client.post(f"/v1/pipelines/{pid}/restart", headers=h)
    assert r.status_code == 409 and any(code == "step_attempts_exhausted" for _, code in _errs(r))
    monkeypatch.setattr(settings, "PIPELINE_MAX_STEP_ATTEMPTS", 5)
    monkeypatch.setattr(settings, "PIPELINE_MAX_RESTARTS", 0)
    r = await client.post(f"/v1/pipelines/{pid}/restart", headers=h)
    assert r.status_code == 409 and ("pipeline", "restart_limit_reached") in _errs(r)


async def test_too_many_active_pipelines(client, graph_only, monkeypatch):
    monkeypatch.setattr(settings, "PIPELINE_MAX_ACTIVE_PER_USER", 1)
    h = await _login(client, "pl-active@example.com")
    assert (await client.post("/v1/pipelines", headers=h, json={"spec": _chain()})).status_code == 201
    r = await client.post("/v1/pipelines", headers=h, json={"spec": _chain()})
    assert r.status_code == 429 and ("pipeline", "too_many_active_pipelines") in _errs(r)
