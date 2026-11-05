"""U9: pipeline graph validation, step state machine, restart from the failed step, cancel (ADR-0006 U9.1, U9.2).

Written, NOT run. See docs/architecture/U9-pipelines-design.md ("Tests"). The state tests replace the database
helpers with in-memory fakes so they check the pipeline logic only.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.models.pipelines import (
    RUN_CANCELLED, RUN_FAILED, RUN_RUNNING, RUN_SUCCEEDED, STEP_CANCELLED, STEP_FAILED, STEP_PENDING,
    STEP_RUNNING, STEP_SKIPPED, STEP_SUCCEEDED,
)
from app.services import job_fsm as fsm
from app.services import pipelines as svc

SRC = "dataset:11111111-1111-4111-8111-111111111111"


def _step(sid, kind="job", **spec):
    return {"id": sid, "kind": kind, "spec": {"version": 2, "kit": f"{sid}@1.0.0", **spec}}


def _chain():
    return {
        "version": 1, "name": "r-u-e",
        "steps": [_step("render", inputs=[{"name": "scene", "source": SRC}]), _step("upscale"), _step("encode")],
        "edges": [{"from": "render.frames", "to": "upscale.images"}, {"from": "upscale.upscaled", "to": "encode.frames"}],
    }


def _codes(exc):
    return {(e["field"], e["code"]) for e in exc.value.errors}


# ---- graph validation ----------------------------------------------------------------------

def test_valid_chain_topological_order():
    vp = svc.parse_graph(_chain())
    assert vp.order == ["render", "upscale", "encode"]
    assert vp.edges[0] == {"from_step": "render", "output": "frames", "to_step": "upscale", "input": "images"}


def test_cycle_refused():
    raw = _chain()
    raw["edges"].append({"from": "encode.video", "to": "render.scene2"})
    with pytest.raises(svc.PipelineError) as e:
        svc.parse_graph(raw)
    assert ("edges", "cycle") in _codes(e)


def test_self_edge_unknown_step_duplicate_id_and_unknown_field():
    raw = _chain()
    raw["edges"] = [{"from": "render.a", "to": "render.b"}, {"from": "nope.a", "to": "encode.b"}, {"from": "x", "to": "y"}]
    raw["steps"].append(_step("render"))
    raw["bogus"] = 1
    with pytest.raises(svc.PipelineError) as e:
        svc.parse_graph(raw)
    got = _codes(e)
    assert ("edges[0]", "self_edge") in got
    assert ("edges[1].from", "unknown_step") in got
    assert ("edges[2].from", "invalid_endpoint") in got
    assert ("steps[3].id", "duplicate_id") in got
    assert ("bogus", "unknown_field") in got


def test_max_steps_cap(monkeypatch):
    monkeypatch.setattr(svc.settings, "PIPELINE_MAX_STEPS", 2)
    with pytest.raises(svc.PipelineError) as e:
        svc.parse_graph(_chain())
    assert ("steps", "too_many_steps") in _codes(e)


def test_input_fed_twice_and_fanout_edge_and_kind_rules():
    raw = _chain()
    raw["edges"].append({"from": "render.frames", "to": "upscale.images"})
    raw["steps"][0]["spec"]["inputs"].append({"name": "images2", "source": SRC})
    raw["edges"].append({"from": "render.frames", "to": "encode.scene"})
    raw["steps"].append(_step("fan", kind="fanout"))
    raw["edges"].append({"from": "fan.out", "to": "encode.other"})
    with pytest.raises(svc.PipelineError) as e:
        svc.parse_graph(raw)
    got = _codes(e)
    assert ("edges[2].to", "input_fed_twice") in got
    assert ("steps[3].spec.parallelism", "parallelism_required") in got
    assert ("edges[4].from", "fanout_output_not_mappable") in got


def test_slot_compatibility():
    assert svc.slot_incompatibility({"name": "o"}, {"name": "i", "kind": "dataset"}) is None
    assert svc.slot_incompatibility({"name": "o"}, {"name": "i", "kind": "stream"}) is not None
    # every shipped Kit manifest declares its inputs as kind "file": an edge into one must not be refused as a wrong kind
    assert svc.slot_incompatibility({"name": "o"}, {"name": "i", "kind": "file", "required": True}) is None
    assert svc.slot_incompatibility({"content_type": "image/png"}, {"kind": "dataset", "accepts": ["image/*"]}) is None
    assert svc.slot_incompatibility({"content_type": "video/mp4"}, {"kind": "dataset", "accepts": "image/png"}) is not None


@pytest.mark.asyncio
async def test_privacy_tier_refused():
    with pytest.raises(svc.PipelineError) as e:
        await svc.validate_pipeline(None, uuid.uuid4(), _chain(), isolation_tier="sealed")
    assert e.value.code == "privacy_tier_not_supported_for_pipelines"


# ---- state machine with in-memory fakes ----------------------------------------------------

class FakeDb:
    async def flush(self):
        pass

    def begin_nested(self):
        class _Ctx:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False
        return _Ctx()


def _mk_run(status=RUN_RUNNING):
    return SimpleNamespace(id=uuid.uuid4(), customer_id=uuid.uuid4(), status=status, restart_count=0, error_code=None,
                           finished_at=None, outputs=None, spec={"steps": [{"id": k} for k in ("a", "b", "c")]})


def _mk_step(key, needs=(), status=STEP_PENDING):
    return SimpleNamespace(step_key=key, kind="job", needs=[{"from_step": n, "output": "o", "input": "i"} for n in needs],
                           status=status, job_id=None, attempts=0, outputs=None, history=[], error_code=None,
                           started_at=None, finished_at=None, spec={})


@pytest.fixture
def world(monkeypatch):
    w = SimpleNamespace(steps=[_mk_step("a"), _mk_step("b", ["a"]), _mk_step("c", ["b"])], jobs={}, started=[], alive=True)

    async def load_steps(db, run):
        return w.steps

    async def load_jobs(db, steps):
        return w.jobs

    async def start_step(db, run, step, by_key):
        jid = uuid.uuid4()
        w.jobs[jid] = SimpleNamespace(id=jid, status=fsm.QUEUED, error_code=None)
        step.job_id, step.status, step.attempts = jid, STEP_RUNNING, step.attempts + 1
        w.started.append(step.step_key)

    async def collect(db, run, job):
        return {"o": {"file_id": str(uuid.uuid4()), "sha256": "x", "size_bytes": 1}}

    async def alive(db, outputs):
        return w.alive

    async def cancel_job(db, step, job):
        job.status = fsm.CANCELLED

    monkeypatch.setattr(svc, "load_steps", load_steps)
    monkeypatch.setattr(svc, "_load_jobs", load_jobs)
    monkeypatch.setattr(svc, "_start_step", start_step)
    monkeypatch.setattr(svc, "_collect_outputs", collect)
    monkeypatch.setattr(svc, "_files_alive", alive)
    monkeypatch.setattr(svc, "_cancel_step_job", cancel_job)
    return w


def _finish(w, key, job_status, error_code=None):
    s = next(x for x in w.steps if x.step_key == key)
    w.jobs[s.job_id].status = job_status
    w.jobs[s.job_id].error_code = error_code


@pytest.mark.asyncio
async def test_chain_runs_in_order_and_succeeds(world):
    run, db = _mk_run(), FakeDb()
    await svc.advance_run(db, run)
    assert world.started == ["a"]  # b and c wait for their inputs
    _finish(world, "a", fsm.COMPLETED)
    await svc.advance_run(db, run)
    assert world.started == ["a", "b"]
    _finish(world, "b", fsm.COMPLETED)
    await svc.advance_run(db, run)
    _finish(world, "c", fsm.COMPLETED)
    await svc.advance_run(db, run)
    assert run.status == RUN_SUCCEEDED and set(run.outputs) == {"c"}


@pytest.mark.asyncio
async def test_failure_skips_downstream_and_restart_reuses_upstream(world):
    run, db = _mk_run(), FakeDb()
    await svc.advance_run(db, run)
    _finish(world, "a", fsm.COMPLETED)
    await svc.advance_run(db, run)
    a_job = world.steps[0].job_id
    _finish(world, "b", fsm.FAILED, "oom")
    await svc.advance_run(db, run)
    assert [s.status for s in world.steps] == [STEP_SUCCEEDED, STEP_FAILED, STEP_SKIPPED]
    assert world.steps[2].error_code == "upstream_failed"
    assert run.status == RUN_FAILED and run.error_code == "step_failed:b"

    info = await svc.restart_run(db, run)
    assert info["restarted_steps"] == ["b", "c"] and info["kept_steps"] == ["a"]
    assert world.steps[0].status == STEP_SUCCEEDED and world.steps[0].job_id == a_job  # not re-run, not re-billed
    await svc.advance_run(db, run)
    assert world.started == ["a", "b", "b"]  # a was never started again
    assert world.steps[1].history[0]["error_code"] == "oom"


@pytest.mark.asyncio
async def test_restart_refused_unless_failed_and_attempt_cap(world, monkeypatch):
    run, db = _mk_run(RUN_RUNNING), FakeDb()
    with pytest.raises(svc.PipelineError) as e:
        await svc.restart_run(db, run)
    assert e.value.code == "not_restartable"
    run.status = RUN_FAILED
    world.steps[0].status = STEP_FAILED
    world.steps[0].attempts = 5
    monkeypatch.setattr(svc.settings, "PIPELINE_MAX_STEP_ATTEMPTS", 5)
    with pytest.raises(svc.PipelineError) as e:
        await svc.restart_run(db, run)
    assert e.value.code == "step_attempts_exhausted"


@pytest.mark.asyncio
async def test_restart_resets_upstream_whose_files_are_gone(world):
    run, db = _mk_run(RUN_FAILED), FakeDb()
    world.steps[0].status, world.steps[0].outputs, world.steps[0].attempts = STEP_SUCCEEDED, {"o": {}}, 1
    world.steps[1].status, world.steps[1].attempts = STEP_FAILED, 1
    world.alive = False
    info = await svc.restart_run(db, run)
    assert info["reset_for_missing_output"] == ["a"] and "a" in info["restarted_steps"]


@pytest.mark.asyncio
async def test_cancel_cancels_running_and_waiting(world):
    run, db = _mk_run(), FakeDb()
    await svc.advance_run(db, run)
    await svc.cancel_run(db, run)
    assert run.status == RUN_CANCELLED
    assert all(s.status == STEP_CANCELLED for s in world.steps)
    assert next(iter(world.jobs.values())).status == fsm.CANCELLED
    await svc.cancel_run(db, run)  # idempotent
    with pytest.raises(svc.PipelineError):
        await svc.restart_run(db, run)


def test_documented_example_matches_the_shipped_kits():
    """docs/examples/pipeline-render-upscale-encode.json named Kits and slots that do not exist."""
    import json
    import pathlib

    import jsonschema

    root = pathlib.Path(__file__).resolve().parents[3]
    raw = json.loads((root / "docs/examples/pipeline-render-upscale-encode.json").read_text())
    vp = svc.parse_graph(raw)
    manifests = {}
    for step in vp.steps:
        slug, _, version = step["spec"]["kit"].partition("@")
        m = json.loads((root / "kits" / slug / "kit.json").read_text())
        assert m["version"] == version and m["mode"] == "batch"
        jsonschema.validate(step["spec"].get("params", {}), m["param_schema"])
        manifests[step["id"]] = m
    for e in vp.edges:
        out = next(o for o in manifests[e["from_step"]]["outputs"] if o["name"] == e["output"])
        inp = next(i for i in manifests[e["to_step"]]["inputs"] if i["name"] == e["input"])
        assert svc.slot_incompatibility(out, inp) is None


def test_trailing_newline_ids_and_image_refs_are_refused():
    """``re.match`` with ``$`` accepts a trailing newline; the id / image checks use fullmatch (review-all)."""
    from app.services import image_scan, kits, pipelines, scheduler_v2, sessions, workload_images

    good = "a/b@sha256:" + "0" * 64
    for rx in (kits.IMAGE_RE, sessions.IMAGE_RE, image_scan._SAFE_REF):
        assert rx.fullmatch(good) and not rx.fullmatch(good + "\n")
    assert pipelines._ID_RE.fullmatch("step_1") and not pipelines._ID_RE.fullmatch("step_1\n")
    assert not scheduler_v2.sanitize_kit_caches(["kit@1.0\n"])
    assert scheduler_v2.sanitize_kit_caches(["kit@1.0"]) == ["kit@1.0"]
    assert workload_images._DIGEST_RE.fullmatch(good) and not workload_images._DIGEST_RE.fullmatch(good + "\n")
