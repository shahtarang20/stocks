"""Pipelines (U9, docs/architecture/U9-pipelines-design.md): a DAG of v2 kit jobs and fan-outs.

Each step is an ordinary job (or a U4 fan-out) created in QUEUED; the existing recovery sweep schedules and holds
it, ``settle`` bills it, ``billing.release_hold`` frees it on cancel. This module never writes to the ledger and
never prices anything. A step's named outputs are data-plane files (U2); an edge feeds one of them to a later
step's input as ``dataset:<file id>``. A restart reuses the recorded outputs of succeeded steps and never creates
a second job for them, so nothing is billed twice.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import re
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.models.data_plane import OBJECT_READY, DataFile, DataObject
from app.models.fanout import FanoutGroup
from app.models.jobs import Job
from app.models.pipelines import (
    KIND_FANOUT,
    KIND_JOB,
    RUN_ACTIVE,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_PENDING,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STEP_CANCELLED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    PipelineRun,
    PipelineStep,
)
from app.services import fanout as fo
from app.services import job_fsm as fsm
from app.services import scheduler_v2
from app.services.kits import KitError, validate_job_spec_v2

logger = logging.getLogger("computecoin.pipelines")

HARD_MAX_STEPS = 50
MAX_EDGES = 200
OUTPUT_GRACE_SECONDS = 300  # a COMPLETED job's output files may still be finishing verification
PLACEHOLDER_FILE_ID = "00000000-0000-4000-8000-000000000000"
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_END_RE = re.compile(r"^([a-z][a-z0-9_]{0,31})\.([a-z][a-z0-9_]{0,31})$")
_PIPE_KEYS = {"version", "name", "steps", "edges"}
_STEP_KEYS = {"id", "kind", "spec"}
_EDGE_KEYS = {"from", "to"}
TERMINAL_JOB_FAILED = fo.CHILD_TERMINAL_FAILED


class PipelineError(Exception):
    """Refusal; ``errors`` is a list of {field, code, message} like KitError."""

    def __init__(self, code: str, status: int = 422, errors: list[dict] | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = status
        self.errors = errors or [_e("pipeline", code)]


def _e(field_: str, code: str, message: str | None = None) -> dict:
    return {"field": field_, "code": code, "message": message or code}


def max_steps() -> int:
    return max(1, min(int(settings.PIPELINE_MAX_STEPS), HARD_MAX_STEPS))


# ---------------------------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------------------------
@dataclass
class ValidatedPipeline:
    name: str | None
    steps: list[dict]  # [{"id", "kind", "spec"}] in submitted order
    edges: list[dict]  # [{"from_step","output","to_step","input"}]
    order: list[str]  # topological order
    kits: dict[str, Any] = field(default_factory=dict)


def parse_graph(raw: Any, *, limit: int | None = None) -> ValidatedPipeline:
    """Structural validation with no database: shapes, ids, caps, endpoints, acyclic. Raises PipelineError(422)."""
    limit = limit or max_steps()
    if not isinstance(raw, dict):
        raise PipelineError("invalid_pipeline", 422, [_e("spec", "must_be_object")])
    errors: list[dict] = []
    for k in raw:
        if k not in _PIPE_KEYS:
            errors.append(_e(k, "unknown_field"))
    if raw.get("version") != 1:
        errors.append(_e("version", "unsupported_version", "version must be 1"))
    name = raw.get("name")
    if name is not None and (not isinstance(name, str) or not 1 <= len(name) <= 128):
        errors.append(_e("name", "invalid_name"))
        name = None
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        errors.append(_e("steps", "must_be_non_empty_list"))
        steps_raw = []
    elif len(steps_raw) > limit:
        errors.append(_e("steps", "too_many_steps", f"at most {limit} steps"))
        steps_raw = steps_raw[:limit]
    steps: list[dict] = []
    ids: set[str] = set()
    for i, s in enumerate(steps_raw):
        f = f"steps[{i}]"
        if not isinstance(s, dict):
            errors.append(_e(f, "must_be_object"))
            continue
        for k in s:
            if k not in _STEP_KEYS:
                errors.append(_e(f"{f}.{k}", "unknown_field"))
        sid = s.get("id")
        if not isinstance(sid, str) or not _ID_RE.fullmatch(sid):
            errors.append(_e(f"{f}.id", "invalid_id"))
            continue
        if sid in ids:
            errors.append(_e(f"{f}.id", "duplicate_id"))
            continue
        ids.add(sid)
        kind = s.get("kind", KIND_JOB)
        if kind not in (KIND_JOB, KIND_FANOUT):
            errors.append(_e(f"{f}.kind", "invalid_kind", "kind must be 'job' or 'fanout'"))
            continue
        spec = s.get("spec")
        if not isinstance(spec, dict):
            errors.append(_e(f"{f}.spec", "must_be_object"))
            continue
        if kind == KIND_FANOUT and spec.get("parallelism") is None:
            errors.append(_e(f"{f}.spec.parallelism", "parallelism_required"))
        if kind == KIND_JOB and "parallelism" in spec:
            errors.append(_e(f"{f}.spec.parallelism", "not_supported_yet", "use kind 'fanout' for a fan-out step"))
        steps.append({"id": sid, "kind": kind, "spec": spec})

    edges_raw = raw.get("edges", [])
    if not isinstance(edges_raw, list):
        errors.append(_e("edges", "must_be_list"))
        edges_raw = []
    elif len(edges_raw) > MAX_EDGES:
        errors.append(_e("edges", "too_many_edges", f"at most {MAX_EDGES} edges"))
        edges_raw = edges_raw[:MAX_EDGES]
    by_id = {s["id"]: s for s in steps}
    edges: list[dict] = []
    fed: set[tuple[str, str]] = set()
    for j, ed in enumerate(edges_raw):
        f = f"edges[{j}]"
        if not isinstance(ed, dict):
            errors.append(_e(f, "must_be_object"))
            continue
        for k in ed:
            if k not in _EDGE_KEYS:
                errors.append(_e(f"{f}.{k}", "unknown_field"))
        ends = []
        for side in ("from", "to"):
            m = _END_RE.fullmatch(ed.get(side)) if isinstance(ed.get(side), str) else None
            if m is None:
                errors.append(_e(f"{f}.{side}", "invalid_endpoint", "use '<step>.<name>'"))
                ends = []
                break
            if m.group(1) not in ids:
                errors.append(_e(f"{f}.{side}", "unknown_step"))
                ends = []
                break
            ends.append(m.groups())
        if len(ends) != 2:
            continue
        (fs, fout), (ts, tin) = ends
        if fs == ts:
            errors.append(_e(f, "self_edge"))
            continue
        if by_id[fs]["kind"] == KIND_FANOUT:
            errors.append(_e(f"{f}.from", "fanout_output_not_mappable",
                             "a fan-out step's merged output is many files and cannot feed an input yet"))
            continue
        if (ts, tin) in fed:
            errors.append(_e(f"{f}.to", "input_fed_twice"))
            continue
        own = by_id[ts]["spec"].get("inputs")
        if isinstance(own, list) and any(isinstance(x, dict) and x.get("name") == tin for x in own):
            errors.append(_e(f"{f}.to", "input_fed_twice", "the step's own inputs already name it"))
            continue
        fed.add((ts, tin))
        edges.append({"from_step": fs, "output": fout, "to_step": ts, "input": tin})

    order = _toposort([s["id"] for s in steps], edges)
    if order is None:
        errors.append(_e("edges", "cycle", "the pipeline graph must be acyclic"))
        order = []
    if errors:
        raise PipelineError("invalid_pipeline", 422, errors)
    return ValidatedPipeline(name=name, steps=steps, edges=edges, order=order)


def _toposort(ids: list[str], edges: list[dict]) -> list[str] | None:
    indeg = {i: 0 for i in ids}
    out: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        indeg[e["to_step"]] += 1
        out[e["from_step"]].append(e["to_step"])
    ready = [i for i in ids if indeg[i] == 0]
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for m in out[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
    return order if len(order) == len(ids) else None


def _type_matches(content_type: str, accepts: Any) -> bool:
    acc = [accepts] if isinstance(accepts, str) else list(accepts or [])
    for a in acc:
        if not isinstance(a, str):
            continue
        if a == content_type or a == "*/*" or (a.endswith("/*") and content_type.startswith(a[:-1])):
            return True
    return False


def slot_incompatibility(out_slot: dict, in_slot: dict) -> str | None:
    """None when a Kit output can feed a Kit input, else a message. Uses only what manifests declare today."""
    # Real Kit manifests declare their input slots as kind "file" (all 12 shipped ones); "dataset" is the older spelling.
    if in_slot.get("kind", "dataset") not in ("dataset", "file"):
        return f"input slot kind is {in_slot.get('kind')!r}, a pipeline edge delivers a dataset file"
    ct, acc = out_slot.get("content_type"), in_slot.get("accepts")
    if ct is not None and acc is not None and not _type_matches(str(ct), acc):
        return f"output type {ct!r} is not accepted by the input ({acc!r})"
    return None


def _placeholder_spec(step: dict, edges: list[dict]) -> dict:
    spec = copy.deepcopy(step["spec"])
    # A step that omits "inputs" (the natural way to write a step whose inputs all come from edges) is the same as []:
    # without this the edge-fed inputs were never injected and validation said "input 'x' is required".
    inputs = list(spec.get("inputs") or []) if isinstance(spec.get("inputs"), (list, type(None))) else spec.get("inputs")
    if isinstance(inputs, list):
        for e in edges:
            if e["to_step"] == step["id"]:
                inputs.append({"name": e["input"], "source": f"dataset:{PLACEHOLDER_FILE_ID}"})
        spec["inputs"] = inputs
    return spec


async def validate_pipeline(db: AsyncSession, customer_id: uuid.UUID, raw: Any, *, isolation_tier: str = "container") -> ValidatedPipeline:
    """Full validation: graph, every step's Job Spec v2 (U1 validator), edge slots against Kit manifests, own inputs."""
    from app.api.routers.jobs_v2 import build_v2_plan

    if isolation_tier not in ("container", "open"):
        raise PipelineError("privacy_tier_not_supported_for_pipelines", 422,
                            [_e("isolation_tier", "privacy_tier_not_supported_for_pipelines")])
    vp = parse_graph(raw)
    errors: list[dict] = []
    validated: dict[str, Any] = {}
    for i, step in enumerate(vp.steps):
        try:
            validated[step["id"]] = await validate_job_spec_v2(
                db, _placeholder_spec(step, vp.edges), allow_parallelism=(step["kind"] == KIND_FANOUT))
        except KitError as exc:
            errors += [{**e, "field": f"steps[{i}].spec.{e.get('field', '')}".rstrip(".")} for e in exc.errors]
    idx_of = {s["id"]: i for i, s in enumerate(vp.steps)}
    for j, e in enumerate(vp.edges):
        src, dst = validated.get(e["from_step"]), validated.get(e["to_step"])
        if src is None or dst is None:
            continue  # the step's own errors are already reported
        out_slot = next((o for o in src.kit.manifest["outputs"] if o["name"] == e["output"]), None)
        in_slot = next((o for o in dst.kit.manifest["inputs"] if o["name"] == e["input"]), None)
        if out_slot is None:
            errors.append(_e(f"edges[{j}].from", "unknown_output", f"{src.kit.slug} has no output '{e['output']}'"))
            continue
        if in_slot is None:
            errors.append(_e(f"edges[{j}].to", "unknown_input", f"{dst.kit.slug} has no input '{e['input']}'"))
            continue
        wanted = {o.name for o in src.spec.outputs}
        if wanted and e["output"] not in wanted:
            errors.append(_e(f"edges[{j}].from", "output_not_selected",
                             f"list '{e['output']}' in steps[{idx_of[e['from_step']]}].spec.outputs"))
            continue
        why = slot_incompatibility(out_slot, in_slot)
        if why:
            errors.append(_e(f"edges[{j}]", "incompatible_slot", why))
    # The customer's own dataset inputs must be live files of theirs (edge-fed inputs are filled in at start).
    for i, step in enumerate(vp.steps):
        v = validated.get(step["id"])
        if v is None:
            continue
        fed = {e["input"] for e in vp.edges if e["to_step"] == step["id"]}
        own = [(k, r) for k, r in enumerate(v.spec.inputs) if r.name not in fed]
        try:
            await build_v2_plan(db, customer_id, SimpleNamespace(inputs=[r for _, r in own], outputs=v.spec.outputs), v.kit)
        except KitError as exc:
            for e in exc.errors:
                m = re.match(r"inputs\[(\d+)\](.*)", e.get("field", ""))
                k = own[int(m.group(1))][0] if m and int(m.group(1)) < len(own) else 0
                errors.append({**e, "field": f"steps[{i}].spec.inputs[{k}]{m.group(2) if m else ''}"})
    if errors:
        raise PipelineError("invalid_pipeline", 422, errors)
    vp.kits = {k: v.kit for k, v in validated.items()}
    return vp


# ---------------------------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------------------------
async def create_run(
    db: AsyncSession, *, customer_id: uuid.UUID, raw: Any, project_id: uuid.UUID | None, priority: int,
    isolation_tier: str, required_gpu_runtime: str, idempotency_key: str | None,
) -> PipelineRun:
    """Validate and persist a run (status pending, every step pending). No job is created here."""
    vp = await validate_pipeline(db, customer_id, raw, isolation_tier=isolation_tier)
    active = await db.scalar(
        select(func.count()).select_from(PipelineRun).where(
            PipelineRun.customer_id == customer_id, PipelineRun.status.in_(RUN_ACTIVE)))
    if int(active or 0) >= int(settings.PIPELINE_MAX_ACTIVE_PER_USER):
        raise PipelineError("too_many_active_pipelines", 429, [_e("pipeline", "too_many_active_pipelines")])
    run = PipelineRun(
        customer_id=customer_id, project_id=project_id, name=vp.name, status=RUN_PENDING, spec=raw,
        edges=vp.edges, priority=priority, isolation_tier=isolation_tier,
        required_gpu_runtime=required_gpu_runtime.lower(), idempotency_key=idempotency_key,
    )
    db.add(run)
    await db.flush()
    for s in vp.steps:
        db.add(PipelineStep(
            run_id=run.id, step_key=s["id"], kind=s["kind"], spec=s["spec"], status=STEP_PENDING,
            needs=[{"from_step": e["from_step"], "output": e["output"], "input": e["input"]}
                   for e in vp.edges if e["to_step"] == s["id"]],
            history=[],
        ))
    await db.flush()
    return run


# ---------------------------------------------------------------------------------------------
# Loading and small helpers
# ---------------------------------------------------------------------------------------------
async def load_steps(db: AsyncSession, run: PipelineRun) -> list[PipelineStep]:
    rows = list((await db.execute(select(PipelineStep).where(PipelineStep.run_id == run.id))).scalars().all())
    pos = {s.get("id"): i for i, s in enumerate((run.spec or {}).get("steps") or []) if isinstance(s, dict)}
    return sorted(rows, key=lambda s: pos.get(s.step_key, 10**6))


async def _load_jobs(db: AsyncSession, steps: list[PipelineStep]) -> dict[uuid.UUID, Job]:
    ids = [s.job_id for s in steps if s.job_id is not None]
    if not ids:
        return {}
    return {j.id: j for j in (await db.execute(select(Job).where(Job.id.in_(ids)))).scalars().all()}


def _downstream(steps: list[PipelineStep]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {s.step_key: set() for s in steps}
    for s in steps:
        for n in s.needs or []:
            if n["from_step"] in out:
                out[n["from_step"]].add(s.step_key)
    return out


def _file_info(f: DataFile) -> dict:
    return {"file_id": str(f.id), "sha256": f.sha256, "size_bytes": int(f.size_bytes)}


async def _files_alive(db: AsyncSession, outputs: dict | None) -> bool:
    now = utcnow()
    for info in (outputs or {}).values():
        try:
            fid = uuid.UUID(info["file_id"])
        except (KeyError, ValueError, TypeError):
            return False
        f = await db.get(DataFile, fid)
        if f is None or f.deleted_at is not None or f.object_id is None:
            return False
        if f.expires_at is not None and ensure_aware(f.expires_at) <= now:
            return False
    return True


async def _collect_outputs(db: AsyncSession, run: PipelineRun, job: Job) -> dict | None:
    """The job's required output files with their recorded size present in the store, or None when not (all) there."""
    names = [o["name"] for o in ((job.data_policy or {}).get("v2_plan") or {}).get("outputs") or [] if o.get("required")]
    store = fo._store()  # FanoutMergeError when the store is off: the caller treats it as transient
    out: dict[str, dict] = {}
    for name in names:
        f = await fo._output_file(db, run.customer_id, job.id, name)
        if f is None:
            return None
        obj = await db.get(DataObject, f.object_id)
        if obj is None or obj.status != OBJECT_READY:
            return None
        size = await fo._head(store, obj.storage_key)
        if size is None or int(size) != int(f.size_bytes):
            return None
        out[name] = _file_info(f)
    return out


# ---------------------------------------------------------------------------------------------
# Starting a step
# ---------------------------------------------------------------------------------------------
def _step_raw_spec(step: PipelineStep, by_key: dict[str, PipelineStep]) -> dict:
    raw = copy.deepcopy(step.spec)
    inputs = list(raw.get("inputs") or [])
    for n in step.needs or []:
        info = ((by_key[n["from_step"]].outputs) or {}).get(n["output"])
        if not info:
            raise PipelineError("upstream_output_missing", 409, [_e(f"{step.step_key}.inputs.{n['input']}", "upstream_output_missing")])
        inputs.append({"name": n["input"], "source": f"dataset:{info['file_id']}"})
    raw["inputs"] = inputs
    return raw


async def _spawn_job(db: AsyncSession, run: PipelineRun, step: PipelineStep, raw: dict) -> Job:
    """One ordinary v2 job for the step (QUEUED; the recovery sweep schedules and holds it). Same shape as a fan-out part."""
    from app.api.routers.jobs_v2 import build_v2_plan
    from app.services.scheduler import JobValidationError, validate_job

    validated = await validate_job_spec_v2(db, raw)
    spec, kit, eff = validated.spec, validated.kit, validated.effective_resources
    v2_plan = await build_v2_plan(db, run.customer_id, spec, kit)
    gpu_count = int(eff.get("gpu_count") or 1)
    job = Job(
        customer_id=run.customer_id, project_id=run.project_id, workload_type="kit",
        model=f"{kit.slug}@{kit.version}", input={}, status=fsm.CREATED, gpu_count=gpu_count,
        estimated_minutes=scheduler_v2.estimate_minutes(kit.manifest, spec.billing.expected_units, gpu_count),
        max_cost=spec.billing.max_cost, max_runtime_seconds=settings.JOB_MAX_RUNTIME_DEFAULT,
        retry_policy={"max_retries": max(0, int(settings.FANOUT_CHILD_MAX_RETRIES))},
        priority=run.priority, execution_mode="hardware", isolation_tier=run.isolation_tier,
        required_gpu_runtime=run.required_gpu_runtime, network_policy=None,
        data_policy={"v2_plan": v2_plan}, idempotency_key=None, machine_id=None,
        spec_v2={"spec": spec.model_dump(mode="json"), "effective_resources": eff,
                 "kit_manifest_sha256": kit.manifest_sha256, "kit_image": kit.image_digest},
        kit_id=kit.slug, kit_version=kit.version,
    )
    db.add(job)
    await db.flush()
    await fsm.transition(db, job, fsm.VALIDATING, reason=f"pipeline_step_{step.step_key}", actor="system")
    try:
        await validate_job(db, job)
    except JobValidationError as exc:
        await fsm.transition(db, job, fsm.FAILED, reason=exc.code, actor="system")
        job.error_code = exc.code
        job.error_message = f"validation failed: {exc.code}"
    else:
        await fsm.transition(db, job, fsm.QUEUED, reason="validated", actor="system")
    return job


async def _start_step(db: AsyncSession, run: PipelineRun, step: PipelineStep, by_key: dict[str, PipelineStep]) -> None:
    raw = _step_raw_spec(step, by_key)
    if step.kind == KIND_FANOUT:
        parent, _group = await fo.submit_fanout(
            db, customer_id=run.customer_id, spec_raw=raw, project_id=run.project_id, machine_id=None,
            priority=run.priority, isolation_tier=run.isolation_tier,
            required_gpu_runtime=run.required_gpu_runtime, idempotency_key=None,
        )
        job = parent
    else:
        job = await _spawn_job(db, run, step, raw)
    step.job_id = job.id
    step.attempts = int(step.attempts or 0) + 1
    step.status = STEP_RUNNING
    step.started_at = utcnow()
    step.finished_at = None
    step.error_code = None


# ---------------------------------------------------------------------------------------------
# Advance one run
# ---------------------------------------------------------------------------------------------
def _fail_step(step: PipelineStep, code: str) -> None:
    step.status = STEP_FAILED
    step.error_code = (code or "step_failed")[:96]
    step.finished_at = utcnow()


async def _refresh_step(db: AsyncSession, run: PipelineRun, step: PipelineStep, job: Job | None, stats: dict) -> None:
    if job is None:
        _fail_step(step, "job_missing")
        stats["failed"] += 1
        return
    if job.status in TERMINAL_JOB_FAILED:
        _fail_step(step, job.error_code or f"job_{job.status}")
        stats["failed"] += 1
        return
    if job.status != fsm.COMPLETED:
        return
    if step.kind == KIND_FANOUT:
        step.outputs = {}
        step.status, step.finished_at, step.error_code = STEP_SUCCEEDED, utcnow(), None
        stats["succeeded"] += 1
        return
    try:
        outs = await _collect_outputs(db, run, job)
    except fo.FanoutMergeError:
        return  # store unavailable: try again next sweep
    now = utcnow()
    if outs is not None:
        step.outputs = outs
        step.status, step.finished_at, step.error_code = STEP_SUCCEEDED, now, None
        stats["succeeded"] += 1
        return
    # COMPLETED but outputs not there. finished_at doubles as "first noticed" while the step is still RUNNING.
    if step.finished_at is None:
        step.finished_at = now
    elif (now - ensure_aware(step.finished_at)).total_seconds() >= OUTPUT_GRACE_SECONDS:
        _fail_step(step, "output_missing")
        stats["failed"] += 1


async def advance_run(db: AsyncSession, run: PipelineRun, now: dt.datetime | None = None) -> dict:
    """One pass: refresh running steps, skip downstream of a failure, start ready steps, finish the run."""
    stats = {"started": 0, "succeeded": 0, "failed": 0, "skipped": 0}
    if run.status not in RUN_ACTIVE:
        return stats
    steps = await load_steps(db, run)
    by_key = {s.step_key: s for s in steps}
    jobs = await _load_jobs(db, steps)
    if run.status == RUN_PENDING:
        run.status = RUN_RUNNING

    for s in steps:
        if s.status == STEP_RUNNING:
            await _refresh_step(db, run, s, jobs.get(s.job_id) if s.job_id else None, stats)

    failed = [s for s in steps if s.status == STEP_FAILED]
    if failed:
        # Fail fast: start nothing new, skip what is waiting, let running (already paid) steps finish.
        dead = {s.step_key for s in failed}
        changed = True
        while changed:
            changed = False
            for s in steps:
                # `not in dead` matters: without it a pending step downstream of a failure re-qualifies on every pass,
                # `changed` is always True and this loop never ends (it hung the request and the worker sweep).
                if s.status == STEP_PENDING and s.step_key not in dead and any(n["from_step"] in dead for n in s.needs or []):
                    dead.add(s.step_key)
                    changed = True
        for s in steps:
            if s.status == STEP_PENDING:
                s.status = STEP_SKIPPED
                s.error_code = "upstream_failed" if s.step_key in dead else "run_failed"
                s.finished_at = utcnow()
                stats["skipped"] += 1
        if not any(s.status == STEP_RUNNING for s in steps):
            run.status = RUN_FAILED
            run.error_code = f"step_failed:{failed[0].step_key}"[:96]
            run.finished_at = utcnow()
        await db.flush()
        return stats

    running = sum(1 for s in steps if s.status == STEP_RUNNING)
    room = max(1, int(settings.PIPELINE_MAX_PARALLEL_STEPS)) - running
    for s in steps:
        if room <= 0:
            break
        if s.status != STEP_PENDING:
            continue
        if any(by_key[n["from_step"]].status != STEP_SUCCEEDED for n in s.needs or []):
            continue
        try:
            async with db.begin_nested():
                await _start_step(db, run, s, by_key)
        except (PipelineError, KitError, fo.FanoutError) as exc:
            # The step cannot start (input file gone, Kit yanked, ...): it fails with a code, the run fails fast.
            _fail_step(s, exc.code)
            s.attempts = int(s.attempts or 0) + 1
            stats["failed"] += 1
            continue
        except Exception:
            logger.exception("pipeline %s: starting step %s failed (will retry next sweep)", run.id, s.step_key)
            continue
        stats["started"] += 1
        room -= 1

    if steps and all(s.status == STEP_SUCCEEDED for s in steps):
        down = _downstream(steps)
        run.outputs = {s.step_key: (s.outputs or {}) for s in steps if not down[s.step_key]}
        run.status = RUN_SUCCEEDED
        run.error_code = None
        run.finished_at = utcnow()
    elif any(s.status == STEP_FAILED for s in steps):
        pass  # handled on the next pass (a step failed while starting)
    await db.flush()
    return stats


# ---------------------------------------------------------------------------------------------
# Cancel and restart
# ---------------------------------------------------------------------------------------------
async def _cancel_step_job(db: AsyncSession, step: PipelineStep, job: Job | None) -> None:
    if job is None:
        return
    if step.kind == KIND_FANOUT:
        group = (await db.execute(select(FanoutGroup).where(FanoutGroup.parent_job_id == job.id))).scalar_one_or_none()
        if group is not None:
            await fo.cancel_group(db, group)
    else:
        await fo._cancel_child(db, job)


async def cancel_run(db: AsyncSession, run: PipelineRun) -> None:
    """Cancel every running step's job and every waiting step. Idempotent on a finished run."""
    if run.status not in RUN_ACTIVE:
        return
    steps = await load_steps(db, run)
    jobs = await _load_jobs(db, steps)
    for s in steps:
        if s.status == STEP_RUNNING:
            await _cancel_step_job(db, s, jobs.get(s.job_id) if s.job_id else None)
            s.status, s.error_code, s.finished_at = STEP_CANCELLED, "customer_cancel", utcnow()
        elif s.status == STEP_PENDING:
            s.status, s.error_code, s.finished_at = STEP_CANCELLED, "customer_cancel", utcnow()
    run.status = RUN_CANCELLED
    run.error_code = None
    run.finished_at = utcnow()
    await db.flush()


def _archive(step: PipelineStep) -> None:
    hist = list(step.history or [])
    hist.append({"attempt": int(step.attempts or 0), "job_id": str(step.job_id) if step.job_id else None,
                 "status": step.status, "error_code": step.error_code})
    step.history = hist


async def restart_run(db: AsyncSession, run: PipelineRun) -> dict:
    """Restart a FAILED run from its failed steps. Succeeded steps keep their jobs and outputs (no re-run, no re-bill)."""
    if run.status != RUN_FAILED:
        raise PipelineError("not_restartable", 409, [_e("pipeline", "not_restartable", "only a failed pipeline can restart")])
    if int(run.restart_count or 0) >= int(settings.PIPELINE_MAX_RESTARTS):
        raise PipelineError("restart_limit_reached", 409, [_e("pipeline", "restart_limit_reached")])
    steps = await load_steps(db, run)
    if any(s.status == STEP_RUNNING for s in steps):
        raise PipelineError("not_restartable", 409, [_e("pipeline", "not_restartable", "a step is still running")])
    down = _downstream(steps)
    redo = {s.step_key for s in steps if s.status != STEP_SUCCEEDED}
    reset_missing: list[str] = []
    changed = True
    while changed:  # a succeeded step whose files are gone must run again if something still needs them
        changed = False
        for s in steps:
            if s.status == STEP_SUCCEEDED and s.step_key not in redo and (down[s.step_key] & redo) \
                    and not await _files_alive(db, s.outputs):
                redo.add(s.step_key)
                reset_missing.append(s.step_key)
                changed = True
    cap = int(settings.PIPELINE_MAX_STEP_ATTEMPTS)
    exhausted = [s.step_key for s in steps if s.step_key in redo and int(s.attempts or 0) >= cap]
    if exhausted:
        raise PipelineError("step_attempts_exhausted", 409,
                            [_e(f"steps.{k}", "step_attempts_exhausted", f"step '{k}' already ran {cap} times") for k in exhausted])
    for s in steps:
        if s.step_key in redo:
            if s.attempts:
                _archive(s)
            s.status, s.job_id, s.outputs, s.error_code = STEP_PENDING, None, None, None
            s.started_at = s.finished_at = None
    run.status = RUN_RUNNING
    run.restart_count = int(run.restart_count or 0) + 1
    run.error_code = None
    run.finished_at = None
    await db.flush()
    return {"restarted_steps": sorted(redo), "reset_for_missing_output": sorted(reset_missing),
            "kept_steps": sorted(s.step_key for s in steps if s.step_key not in redo)}


# ---------------------------------------------------------------------------------------------
# Worker sweep
# ---------------------------------------------------------------------------------------------
async def sweep_pipelines(sm: Any) -> dict:
    """Worker step. One transaction and one try/except per run: a broken run never blocks the others."""
    totals: dict[str, int] = {"runs": 0, "started": 0, "succeeded": 0, "failed": 0, "skipped": 0, "errors": 0}
    async with sm() as db:
        ids = list((await db.execute(
            select(PipelineRun.id).where(PipelineRun.status.in_(RUN_ACTIVE)).order_by(PipelineRun.created_at).limit(200)
        )).scalars().all())
    for rid in ids:
        try:
            async with sm() as db:
                run = (await db.execute(
                    select(PipelineRun).where(PipelineRun.id == rid).with_for_update(skip_locked=True)
                )).scalar_one_or_none()
                if run is None or run.status not in RUN_ACTIVE:
                    continue
                s = await advance_run(db, run)
                await db.commit()
            totals["runs"] += 1
            for k, v in s.items():
                totals[k] = totals.get(k, 0) + v
        except Exception:
            logger.exception("pipeline sweep failed for run %s (will retry next sweep)", rid)
            totals["errors"] += 1
    return totals
