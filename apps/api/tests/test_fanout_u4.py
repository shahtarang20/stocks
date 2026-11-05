"""U4: fan-out planner, part runs, straggler winner and merge (ADR-0006 checkpoints U4.1-U4.3).

Written, NOT run. See docs/architecture/U4-fan-out-design.md ("Tests").
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.fanout import (
    GROUP_FAILED,
    GROUP_RUNNING,
    FanoutGroup,
    FanoutPart,
    FanoutPartRun,
)
from app.models.jobs import Job
from app.models.user import User
from app.schemas.job_spec_v2 import JobSpecV2, Parallelism
from app.services import fanout as svc
from app.services import job_fsm as fsm
from app.services.kits import validate_manifest
from tests.helpers import auth_headers, register_verify_login

MANIFEST = {"split": {"range": {"param": "frame_range"}, "items": {"param": "prompts"}, "bytes": {"param": "window"}}}


def _par(split, **kw):
    return Parallelism.model_validate({"split": split, **kw})


# ---- planner (pure) ------------------------------------------------------------------------

def test_range_500_frames_in_order():
    plans = svc.plan_parts(manifest=MANIFEST, split=_par({"kind": "range", "from": 1, "to": 500, "chunk": 25}).split,
                           params={}, input_sizes={})
    assert len(plans) == 20
    assert [p.index for p in plans] == list(range(20))
    assert plans[0].patch == {"frame_range": {"from": 1, "to": 25}}
    assert plans[-1].patch == {"frame_range": {"from": 476, "to": 500}}
    assert sum(p.weight for p in plans) == 500


def test_range_uneven_last_chunk():
    plans = svc.plan_parts(manifest=MANIFEST, split=_par({"kind": "range", "from": 1, "to": 10, "chunk": 4}).split,
                           params={}, input_sizes={})
    assert [(p.patch["frame_range"]["from"], p.patch["frame_range"]["to"]) for p in plans] == [(1, 4), (5, 8), (9, 10)]


def test_items_and_bytes():
    items = [f"p{i}" for i in range(25)]
    plans = svc.plan_parts(manifest=MANIFEST, split=_par({"kind": "items", "chunk": 10}).split,
                           params={"prompts": items}, input_sizes={})
    assert [len(p.patch["prompts"]) for p in plans] == [10, 10, 5]
    plans = svc.plan_parts(manifest=MANIFEST, split=_par({"kind": "bytes", "input": "scene", "chunk_bytes": 100}).split,
                           params={}, input_sizes={"scene": 250})
    assert [p.patch["window"] for p in plans] == [{"offset": 0, "length": 100}, {"offset": 100, "length": 100}, {"offset": 200, "length": 50}]


def test_bounds_and_refusals():
    # 1001 parts: refused by the schema itself, before any planning.
    with pytest.raises(ValidationError):
        _par({"kind": "range", "from": 1, "to": 1001, "chunk": 1})
    with pytest.raises(ValidationError):
        _par({"kind": "range", "from": 10, "to": 1, "chunk": 1})
    with pytest.raises(ValidationError):
        _par({"kind": "range", "from": 1, "to": 5, "chunk": 0})
    with pytest.raises(ValidationError):
        _par({"kind": "range", "from": 1, "to": 5, "chunk": 1, "max_parallel": 0})
    with pytest.raises(ValidationError):
        _par({"kind": "range", "from": 1, "to": 5, "chunk": 1, "merge": {"kit": "x@1"}})  # no merge Kit yet
    # Kit that does not declare the shape; customer-set reserved param; too many items per the platform limit.
    with pytest.raises(svc.FanoutError) as e:
        svc.plan_parts(manifest={}, split=_par({"kind": "range", "from": 1, "to": 5, "chunk": 1}).split, params={}, input_sizes={})
    assert e.value.code == "kit_split_not_declared"
    with pytest.raises(svc.FanoutError) as e:
        svc.plan_parts(manifest=MANIFEST, split=_par({"kind": "range", "from": 1, "to": 5, "chunk": 1}).split,
                       params={"frame_range": {}}, input_sizes={})
    assert e.value.code == "param_reserved_for_split"
    with pytest.raises(svc.FanoutError) as e:
        svc.plan_parts(manifest=MANIFEST, split=_par({"kind": "range", "from": 1, "to": 50, "chunk": 1}).split,
                       params={}, input_sizes={}, limit=10)
    assert e.value.code == "too_many_parts"


def test_child_spec_drops_parallelism_and_shares_cost():
    base = {"version": 2, "kit": "k@1", "params": {"quality": 3}, "inputs": [], "outputs": [],
            "billing": {"expected_units": 500, "max_cost": "100.00"},
            "parallelism": {"split": {"kind": "range", "from": 1, "to": 500, "chunk": 25}}}
    part = type("P", (), {"patch": {"frame_range": {"from": 1, "to": 25}}, "weight": 25})()
    raw = svc.child_raw_spec(base, part, 500)
    assert "parallelism" not in raw and raw["params"] == {"quality": 3, "frame_range": {"from": 1, "to": 25}}
    assert raw["billing"] == {"expected_units": 25, "max_cost": "5.00"}
    assert "parallelism" in base  # the parent's spec is not mutated
    JobSpecV2.model_validate(raw)


def test_manifest_split_declaration_validated():
    base = {"schema": 1}  # other errors expected; only split-specific ones are asserted
    errs = validate_manifest({**base, "split": {"range": {"param": "Bad Name"}, "grid": {"param": "x"}}})
    fields = {e["field"] for e in errs}
    assert "manifest.split.range" in fields and "manifest.split.grid" in fields
    assert not any(f.startswith("manifest.split") for f in {e["field"] for e in validate_manifest({**base, "split": MANIFEST["split"]})})


# ---- routes ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parallelism_refused_on_single_job_route(client):
    h = auth_headers((await register_verify_login(client, "u4-a@example.com"))["access_token"])
    spec = {"version": 2, "kit": "blender-render@4.2.0",
            "parallelism": {"split": {"kind": "range", "from": 1, "to": 10, "chunk": 5}}}
    r = await client.post("/v1/jobs/v2", headers=h, json={"spec": spec})
    assert r.status_code == 422
    assert any(e["field"] == "parallelism" and e["code"] == "not_supported_yet" for e in r.json()["detail"]["errors"])


@pytest.mark.asyncio
async def test_fanout_refuses_privacy_tier(client):
    h = auth_headers((await register_verify_login(client, "u4-b@example.com"))["access_token"])
    r = await client.post("/v1/jobs/v2/fanout", headers=h, json={"spec": {}, "isolation_tier": "sealed"})
    assert r.status_code in (404, 422, 503)  # data plane off answers first in a bare test environment
    assert r.status_code != 201


# ---- sweep logic with hand-built rows (no Kit, no store) ----------------------------------------

async def _user_id(email: str) -> uuid.UUID:
    async with get_sessionmaker()() as db:
        return (await db.execute(select(User.id).where(User.email == email))).scalar_one()


def _job(uid, status=fsm.RUNNING, **kw) -> Job:
    return Job(customer_id=uid, workload_type="kit", model="k@1", input={}, status=status, gpu_count=1,
               estimated_minutes=1, max_runtime_seconds=300, retry_policy={}, execution_mode="hardware", **kw)


async def _group(db, uid, n, *, max_parallel=0, outputs=None):
    parent = _job(uid)
    parent.workload_type = "kit_fanout"
    db.add(parent)
    await db.flush()
    group = FanoutGroup(parent_job_id=parent.id, customer_id=uid, status=GROUP_RUNNING, part_count=n, split={},
                        plan={"outputs": outputs or [], "max_parallel": max_parallel, "straggler_multiple": 3.0,
                              "max_part_attempts": 2, "total_weight": n})
    db.add(group)
    await db.flush()
    parts = []
    for i in range(n):
        p = FanoutPart(group_id=group.id, idx=i, patch={}, weight=1, status="running", attempts=1)
        db.add(p)
        parts.append(p)
    await db.flush()
    return parent, group, parts


async def _run(db, uid, part, status, kind="primary", seconds=None):
    j = _job(uid, status=status)
    if seconds is not None:
        j.started_at = utcnow() - dt.timedelta(seconds=seconds + 10)
        j.finished_at = utcnow() - dt.timedelta(seconds=10)
        j.metrics = {"duration_seconds": seconds}
        j.final_cost = decimal.Decimal("1.00")
    db.add(j)
    await db.flush()
    r = FanoutPartRun(part_id=part.id, job_id=j.id, kind=kind, status="active")
    db.add(r)
    await db.flush()
    return j, r


@pytest.mark.asyncio
async def test_failed_part_reruns_alone_others_untouched(client, monkeypatch):
    """U4.2: only the failed part goes back to pending; finished parts keep their winner and cost."""
    await register_verify_login(client, "u4-c@example.com")
    uid = await _user_id("u4-c@example.com")
    monkeypatch.setattr(svc, "_store", lambda: object())
    async with get_sessionmaker()() as db:
        # max_parallel=1: part 2 still occupies the window, so the failed part waits as pending (otherwise advance_group
        # would immediately spawn its re-run in the same pass, which needs a real Kit).
        parent, group, parts = await _group(db, uid, 3, max_parallel=1)
        j0, r0 = await _run(db, uid, parts[0], fsm.COMPLETED, seconds=10)
        j1, r1 = await _run(db, uid, parts[1], fsm.FAILED)
        j2, r2 = await _run(db, uid, parts[2], fsm.RUNNING)
        await svc.advance_group(db, group)
        assert parts[0].status == "done" and parts[0].winner_job_id == j0.id and parts[0].final_cost == decimal.Decimal("1.00")
        assert parts[1].status == "pending" and r1.status == "failed" and parts[1].attempts == 1
        assert parts[2].status == "running" and r2.status == "active" and j2.status == fsm.RUNNING
        assert group.status == GROUP_RUNNING


@pytest.mark.asyncio
async def test_part_failing_all_attempts_fails_group_and_cancels_rest(client, monkeypatch):
    await register_verify_login(client, "u4-d@example.com")
    uid = await _user_id("u4-d@example.com")
    monkeypatch.setattr(svc, "_store", lambda: object())
    async with get_sessionmaker()() as db:
        parent, group, parts = await _group(db, uid, 2)
        parts[0].attempts = 2  # max_part_attempts is 2
        await _run(db, uid, parts[0], fsm.FAILED)
        j1, r1 = await _run(db, uid, parts[1], fsm.RUNNING)
        await svc.advance_group(db, group)
        assert group.status == GROUP_FAILED and group.error_code == "fanout_part_failed"
        assert parent.status == fsm.FAILED
        assert j1.status == fsm.CANCELLED and r1.status == "cancelled"


@pytest.mark.asyncio
async def test_first_valid_result_wins_and_loser_is_cancelled(client, monkeypatch):
    """U4.3: a straggler copy and the original both exist; the first COMPLETED one is kept, the other cancelled."""
    await register_verify_login(client, "u4-e@example.com")
    uid = await _user_id("u4-e@example.com")
    monkeypatch.setattr(svc, "_store", lambda: object())
    async with get_sessionmaker()() as db:
        parent, group, parts = await _group(db, uid, 1)
        slow, r_slow = await _run(db, uid, parts[0], fsm.RUNNING)
        copy_, r_copy = await _run(db, uid, parts[0], fsm.COMPLETED, kind="straggler", seconds=5)
        await svc.advance_group(db, group)
        assert parts[0].winner_job_id == copy_.id
        assert r_copy.status == "won" and r_slow.status == "lost"
        assert slow.status == fsm.CANCELLED and slow.cancel_requested is True


@pytest.mark.asyncio
async def test_parent_cancel_cancels_parts(client, monkeypatch):
    await register_verify_login(client, "u4-f@example.com")
    uid = await _user_id("u4-f@example.com")
    async with get_sessionmaker()() as db:
        parent, group, parts = await _group(db, uid, 2)
        j0, r0 = await _run(db, uid, parts[0], fsm.RUNNING)
        j1, r1 = await _run(db, uid, parts[1], fsm.QUEUED)
        await svc.cancel_group(db, group)
        assert group.status == "cancelled" and parent.status == fsm.CANCELLED
        assert j0.status == fsm.CANCELLED and j1.status == fsm.CANCELLED
        assert all(p.status == "cancelled" for p in parts)


# ---- straggler spawn and merge, with a fake data-plane store (WRITTEN, NOT YET RUN) ---------------

import hashlib  # noqa: E402

from app.models.data_plane import OBJECT_READY, DataFile, DataObject  # noqa: E402
from app.models.fanout import GROUP_COMPLETED, GROUP_MERGING, PART_DONE, RUN_STRAGGLER  # noqa: E402


class _FakeStore:
    """The two calls the merge makes (head_size, sha256_of), as in tests/test_u2_data_plane.py's FakeStore."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def head_size(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def sha256_of(self, key):
        return hashlib.sha256(self.objects[key]).hexdigest()


async def _output(db, store, uid, job_id, name, data: bytes, *, stored: bytes | None = None):
    """Record one output file for a part's winner job; ``stored`` differs from ``data`` to fake corruption."""
    key = f"obj/{uuid.uuid4()}"
    store.objects[key] = data if stored is None else stored
    digest = hashlib.sha256(data).hexdigest()
    obj = DataObject(scope_key=str(uid), sha256=digest, size_bytes=len(data), storage_key=key, refcount=1, status=OBJECT_READY)
    db.add(obj)
    await db.flush()
    db.add(DataFile(owner_id=uid, object_id=obj.id, name=f"out/{job_id}/{name}", size_bytes=len(data), sha256=digest))
    await db.flush()


@pytest.mark.asyncio
async def test_straggler_copy_spawned_for_slow_part_only(client, monkeypatch):
    await register_verify_login(client, "u4-g@example.com")
    uid = await _user_id("u4-g@example.com")
    monkeypatch.setattr(svc, "_store", lambda: _FakeStore())
    monkeypatch.setattr(settings, "FANOUT_STRAGGLER_MIN_DONE", 3)
    spawned = []

    async def fake_spawn(db, group, parent, part, kind):
        j = _job(uid, status=fsm.QUEUED)
        db.add(j)
        await db.flush()
        run = FanoutPartRun(part_id=part.id, job_id=j.id, kind=kind, status="active")
        db.add(run)
        await db.flush()
        spawned.append((part.idx, kind))
        return run

    monkeypatch.setattr(svc, "_spawn_run", fake_spawn)
    async with get_sessionmaker()() as db:
        parent, group, parts = await _group(db, uid, 5)
        for p in parts[:3]:
            await _run(db, uid, p, fsm.COMPLETED, seconds=10)  # median 10s -> threshold 30s
        slow, _ = await _run(db, uid, parts[3], fsm.RUNNING)
        slow.started_at = utcnow() - dt.timedelta(seconds=120)
        fresh, _ = await _run(db, uid, parts[4], fsm.RUNNING)
        fresh.started_at = utcnow() - dt.timedelta(seconds=5)  # not slow
        stats = await svc.advance_group(db, group)
        assert stats["stragglers"] == 1 and spawned == [(3, RUN_STRAGGLER)]
        again = await svc.advance_group(db, group)  # at most one copy per part, ever
        assert again["stragglers"] == 0 and spawned == [(3, RUN_STRAGGLER)]


@pytest.mark.asyncio
async def test_no_straggler_copy_before_enough_parts_are_done(client, monkeypatch):
    await register_verify_login(client, "u4-h@example.com")
    uid = await _user_id("u4-h@example.com")
    monkeypatch.setattr(svc, "_store", lambda: _FakeStore())
    monkeypatch.setattr(settings, "FANOUT_STRAGGLER_MIN_DONE", 3)

    async def boom(*a, **k):
        raise AssertionError("no copy expected")

    monkeypatch.setattr(svc, "_spawn_run", boom)
    async with get_sessionmaker()() as db:
        parent, group, parts = await _group(db, uid, 3)
        await _run(db, uid, parts[0], fsm.COMPLETED, seconds=10)
        slow, _ = await _run(db, uid, parts[1], fsm.RUNNING)
        slow.started_at = utcnow() - dt.timedelta(seconds=500)
        await _run(db, uid, parts[2], fsm.RUNNING)
        assert (await svc.advance_group(db, group))["stragglers"] == 0


async def _merging_group(db, uid, store, n, *, corrupt_part: int | None = None):
    parent, group, parts = await _group(db, uid, n, outputs=["frames.bin"])
    group.status = GROUP_MERGING
    for p in parts:
        j = _job(uid, status=fsm.COMPLETED)
        j.final_cost = decimal.Decimal("1.00")
        db.add(j)
        await db.flush()
        p.status, p.winner_job_id, p.final_cost = PART_DONE, j.id, j.final_cost  # as advance_group records a won part
        data = f"frames-{p.idx}".encode()
        await _output(db, store, uid, j.id, "frames.bin", data, stored=(b"x" if corrupt_part == p.idx else None))
    await db.flush()
    return parent, group, parts


@pytest.mark.asyncio
async def test_merge_success_lists_outputs_in_part_order(client, monkeypatch):
    await register_verify_login(client, "u4-i@example.com")
    uid = await _user_id("u4-i@example.com")
    store = _FakeStore()
    monkeypatch.setattr(svc, "_store", lambda: store)
    async with get_sessionmaker()() as db:
        parent, group, parts = await _merging_group(db, uid, store, 3)
        stats = await svc.advance_group(db, group)
        assert stats["merged"] == 1 and group.status == GROUP_COMPLETED
        outs = group.merge_result["fanout"]["outputs"]["frames.bin"]
        assert [o["index"] for o in outs] == [0, 1, 2]
        assert group.merge_result["fanout"]["manifest_sha256"] and parent.status == fsm.COMPLETED
        # Key check (fanout.transparency): the plain ordered_manifest merge carries NO "transparency" key; only the
        # private/sealed merge (fanout_privacy, ordered_ciphertext) adds it, additively. Assert tolerant containment,
        # never exact key equality, so new additive keys do not break this test.
        assert {"parts", "merge", "outputs", "manifest_sha256"} <= set(group.merge_result["fanout"])
        assert "transparency" not in group.merge_result["fanout"]
        assert parent.final_cost == decimal.Decimal("3.00")


@pytest.mark.asyncio
async def test_merge_failure_retries_then_fails_group(client, monkeypatch):
    await register_verify_login(client, "u4-j@example.com")
    uid = await _user_id("u4-j@example.com")
    store = _FakeStore()
    monkeypatch.setattr(svc, "_store", lambda: store)
    monkeypatch.setattr(settings, "FANOUT_MERGE_MAX_ATTEMPTS", 2)
    async with get_sessionmaker()() as db:
        parent, group, parts = await _merging_group(db, uid, store, 2, corrupt_part=1)  # stored size != recorded size
        await svc.advance_group(db, group)
        assert group.status == GROUP_MERGING and group.merge_attempts == 1 and parent.status != fsm.COMPLETED
        await svc.advance_group(db, group)
        assert group.status == GROUP_FAILED and group.error_code == "fanout_merge_failed"
        assert parent.status == fsm.FAILED
