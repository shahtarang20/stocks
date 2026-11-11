"""Audit: the update scheduler must not leave the claim gate's drain flag set after it gives up on an update.

``AgentUpdater.run_once`` asks the gate to drain when a job is running and defers. Only its install path clears that flag, so
a scheduler pass that ended any other way (manifest withdrawn, fetch error, drain timeout) left the host throttled forever
with ``update_gate_closed``.
"""

from __future__ import annotations

from computecoin_agent.updater.core import DEFERRED_JOB
from computecoin_agent.windows.supervisor_wiring import SupervisorUpdateGate, UpdateScheduler
from test_w6_updater import Harness


def _scheduler(tmp_path, jobs, t, **kw):
    h = Harness(tmp_path)
    gate = SupervisorUpdateGate("1.0.0", lambda: jobs[0])
    h.gate = h.updater.gate = gate
    drains = []
    sched = UpdateScheduler(
        h.updater, gate, interval_seconds=100.0, begin_drain=lambda: drains.append("begin"), end_drain=lambda: drains.append("end"),
        active_jobs=lambda: jobs[0], drain_complete=lambda: jobs[0] == 0, clock=lambda: t[0], first_check_delay=0.0, **kw,
    )
    return h, gate, sched, drains


def test_gate_reopens_when_the_manifest_disappears_between_the_drain_and_the_install(tmp_path):
    jobs, t = [1], [1000.0]
    h, gate, sched, drains = _scheduler(tmp_path, jobs, t)
    assert sched.step() is None and drains == ["begin"]  # an update is wanted: draining (the gate drain flag is now set)
    assert not gate.may_claim()
    jobs[0] = 0  # the job finished ...
    h.manifest = None  # ... and the server no longer offers a release
    result = sched.step()
    assert result is not None and result.status != DEFERRED_JOB
    assert gate.may_claim(), "the host stays throttled (update_gate_closed) forever after a withdrawn update"


def test_gate_reopens_after_a_drain_timeout(tmp_path):
    jobs, t = [1], [1000.0]
    h, gate, sched, drains = _scheduler(tmp_path, jobs, t, drain_timeout_seconds=60.0)
    assert sched.step() is None
    t[0] += 61.0  # a long job is still running past the timeout
    result = sched.step()
    assert result is not None and result.code == "drain_timeout"
    assert gate.may_claim() and drains[-1] == "end"
