"""U13: rank-failure sweep (fakes), private-tier refusal and the per-rank cost cap. Written, not run by the author.

Bug re-insertion list: drop the `status not in (placed, running)` re-check in sweep_rank_failures (finished-group test
fails); drop the per-group try/except (isolation test fails); round up instead of down in per_rank_max_cost (cap test
fails); remove the tier check in scheduler.validate_job (tier test fails); copy job.max_cost unchanged in create_group
(sibling cap test fails).
"""

from __future__ import annotations

import decimal
import uuid
from types import SimpleNamespace as NS

import pytest

from app.core.config import settings
from app.models.node_group import GROUP_DONE, GROUP_FAILED, GROUP_PLACED, GROUP_RUNNING
from app.services import job_fsm as fsm
from app.services import multi_node as mn
from app.services import scheduler_v2 as s2

D = decimal.Decimal


class _Res:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def all(self):
        return list(self.value)

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value


class _FakeDB:
    """execute() is scripted by a callable(statement_text) -> value."""

    def __init__(self, script):
        self.script = script
        self.commits = 0

    async def execute(self, stmt):
        return _Res(self.script(str(stmt)))

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass


class _SM:
    def __init__(self, db):
        self.db = db

    def __call__(self):
        outer = self

        class _Ctx:
            async def __aenter__(self_inner):
                return outer.db

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


# ---------------------------------------------------------------- sweep_rank_failures

async def test_sweep_visits_each_live_group_and_commits(monkeypatch):
    g1, g2 = NS(id=uuid.uuid4(), status=GROUP_PLACED), NS(id=uuid.uuid4(), status=GROUP_RUNNING)
    groups = {g1.id: g1, g2.id: g2}
    state = {"ids": [g1.id, g2.id], "next": []}

    def script(sql):
        if "node_groups.id" in sql and "ORDER BY" in sql:
            return state["ids"]
        # group lookup: hand back the groups in the order they were listed
        return groups[state["ids"][len(state["next"])]]

    seen = []

    async def fake_propagate(db, group):
        seen.append(group.id)
        state["next"].append(group.id)
        return 1

    db = _FakeDB(script)
    monkeypatch.setattr(mn, "propagate_group_failure", fake_propagate)
    out = await mn.sweep_rank_failures(_SM(db))
    assert out["errors"] == 0 and out["groups"] == 2 and out["cancelled"] == 2
    assert seen == [g1.id, g2.id] and db.commits == 2


async def test_sweep_skips_group_that_finished_meanwhile(monkeypatch):
    gid = uuid.uuid4()
    done = NS(id=gid, status=GROUP_DONE)
    db = _FakeDB(lambda sql: [gid] if "ORDER BY" in sql else done)
    called = []

    async def fake_propagate(db, group):
        called.append(1)
        return 5

    monkeypatch.setattr(mn, "propagate_group_failure", fake_propagate)
    out = await mn.sweep_rank_failures(_SM(db))
    assert called == [] and out == {"groups": 0, "cancelled": 0, "errors": 0}


async def test_sweep_one_broken_group_does_not_block_the_others(monkeypatch):
    g1, g2 = NS(id=uuid.uuid4(), status=GROUP_PLACED), NS(id=uuid.uuid4(), status=GROUP_PLACED)
    order = [g1, g2]
    pos = {"i": -1}

    def script(sql):
        if "ORDER BY" in sql:
            return [g1.id, g2.id]
        pos["i"] += 1
        return order[pos["i"]]

    async def fake_propagate(db, group):
        if group is g1:
            raise RuntimeError("boom")
        return 2

    monkeypatch.setattr(mn, "propagate_group_failure", fake_propagate)
    out = await mn.sweep_rank_failures(_SM(_FakeDB(script)))
    assert out == {"groups": 1, "cancelled": 2, "errors": 1}


async def test_propagate_cancels_live_siblings_once_and_marks_failed(monkeypatch):
    j_bad = NS(id=uuid.uuid4(), status=fsm.FAILED)
    j_live = NS(id=uuid.uuid4(), status=fsm.RUNNING)
    j_done = NS(id=uuid.uuid4(), status=fsm.COMPLETED)
    jobs = {j.id: j for j in (j_bad, j_live, j_done)}
    members = [NS(job_id=j.id) for j in jobs.values()]
    group = NS(id=uuid.uuid4(), status=GROUP_RUNNING)
    queue = list(jobs.values())

    def script(sql):
        if "node_group_members" in sql:
            return members
        return queue.pop(0)

    cancelled = []

    async def fake_cancel(db, job):
        cancelled.append(job.id)

    monkeypatch.setattr(mn, "_cancel_rank_job", fake_cancel)
    n = await mn.propagate_group_failure(_FakeDB(script), group)
    assert n == 1 and cancelled == [j_live.id] and group.status == GROUP_FAILED
    # a finished group is never swept again
    assert await mn.propagate_group_failure(_FakeDB(script), group) == 0 and cancelled == [j_live.id]


async def test_propagate_marks_done_when_all_ranks_completed():
    js = [NS(id=uuid.uuid4(), status=fsm.COMPLETED) for _ in range(2)]
    members = [NS(job_id=j.id) for j in js]
    queue = list(js)
    group = NS(id=uuid.uuid4(), status=GROUP_RUNNING)
    n = await mn.propagate_group_failure(_FakeDB(lambda sql: members if "node_group_members" in sql else queue.pop(0)), group)
    assert n == 0 and group.status == GROUP_DONE


# ---------------------------------------------------------------- private-tier refusal

@pytest.mark.parametrize("tier", ["sealed", "sealed_pro", "confidential"])
async def test_validate_job_refuses_multi_node_on_private_tiers(monkeypatch, tier):
    from app.services import scheduler

    async def ok(db, job):
        return None

    monkeypatch.setattr(s2, "validate_kit_job", ok)
    monkeypatch.setattr(settings, "MULTI_NODE_ENABLED", True)
    job = NS(spec_v2={"effective_resources": {"gpu_count": 1, "node_count": 2}}, isolation_tier=tier, gpu_count=1,
             execution_mode="hardware", input={}, workload_type="kit", machine_id=None, required_gpu_runtime="cuda",
             max_cost=None)
    with pytest.raises(scheduler.JobValidationError) as ei:
        await scheduler.validate_job(None, job)
    assert "multi_node_private_tier_refused" in str(ei.value.args) + str(getattr(ei.value, "code", ""))


# ---------------------------------------------------------------- per-rank cost cap

@pytest.mark.parametrize("total,n,expected", [
    ("10.00", 2, "5.00"), ("10.00", 3, "3.33"), ("0.99", 4, "0.24"), ("100", 4, "25.00"), ("0.02", 4, "0.01"),
])
def test_per_rank_cap_rounds_down_with_minimum(total, n, expected):
    assert s2.per_rank_max_cost(D(total), n) == D(expected)


@pytest.mark.parametrize("total", ["10.00", "7.77", "1.00", "123.45"])
@pytest.mark.parametrize("n", [2, 3, 4])
def test_total_held_never_exceeds_run_cap(total, n):
    per = s2.per_rank_max_cost(D(total), n)
    assert per * n <= D(total)


def test_absent_max_cost_uses_platform_default_divided():
    assert s2.per_rank_max_cost(None, 4) == (D(settings.MULTI_NODE_DEFAULT_MAX_COST) / 4).quantize(D("0.01"), rounding=decimal.ROUND_DOWN)


async def test_create_group_gives_siblings_the_rank_share(monkeypatch):
    added = []

    class DB:
        def add(self, o):
            added.append(o)

        async def flush(self):
            for o in added:
                if getattr(o, "id", None) is None:
                    o.id = uuid.uuid4()

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(mn.fsm, "transition", noop)
    job = NS(id=uuid.uuid4(), customer_id=uuid.uuid4(), project_id=None, workload_type="kit", model="k@1", input={},
             gpu_count=1, estimated_minutes=10, max_cost=D("3.33"), max_runtime_seconds=300, priority=0,
             execution_mode="hardware", isolation_tier="container", required_gpu_runtime="cuda", network_policy=None,
             data_policy={}, kit_id="k", kit_version="1",
             spec_v2={"hold_estimate": {"run_max_cost": "10.00", "rank_max_cost": "3.33", "node_count": 3}})
    machines = [NS(id=uuid.uuid4()) for _ in range(3)]
    await mn.create_group(DB(), job, "dc1", machines, noop)
    sibs = [o for o in added if o.__class__.__name__ == "Job"]
    assert len(sibs) == 2 and all(s.max_cost == D("3.33") for s in sibs)
    assert sum(s.max_cost for s in sibs) + job.max_cost <= D("10.00")
