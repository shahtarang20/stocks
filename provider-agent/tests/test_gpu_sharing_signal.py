"""While a job runs, the agent notes whether the host owner's own programs were also on the GPU (a yes and a count only)."""

from __future__ import annotations

import types

import pytest

from computecoin_agent import jobrunner
from computecoin_agent.executor import ExecutionResult, JobSpec
from computecoin_agent.jobmonitor import JobMonitor


def _monitor(probe, poll=0.0):
    return JobMonitor("http://api.invalid", "m1", "secret", "job-1", shared_gpu_probe=probe, sharing_poll_seconds=poll)


# ---- the monitor ------------------------------------------------------------------------------------------------
def test_nobody_else_on_the_gpu_reports_nothing_at_all():
    m = _monitor(lambda: [])
    m._observe_sharing()
    assert m.sharing_metrics() == {}  # not "gpu_shared": False: only a sighting is reported


def test_another_program_on_the_gpu_is_reported_as_a_yes_and_a_count_never_a_name():
    m = _monitor(lambda: ["cyberpunk2077", "obs"])
    m._observe_sharing()
    out = m.sharing_metrics()
    assert out == {"gpu_shared": True, "gpu_other_processes": 2}
    assert "cyberpunk2077" not in repr(out) and "obs" not in repr(out)  # the owner's programs are the owner's business


def test_once_seen_it_stays_reported_and_the_count_is_the_most_seen():
    seen = iter([["a"], ["a", "b", "c"], [], None])
    m = _monitor(lambda: next(seen))
    for _ in range(4):
        m._observe_sharing()
    assert m.sharing_metrics() == {"gpu_shared": True, "gpu_other_processes": 3}


def test_a_probe_that_cannot_look_or_crashes_says_nothing_and_never_stops_the_job():
    unknown = _monitor(lambda: None)
    unknown._observe_sharing()
    assert unknown.sharing_metrics() == {}

    def boom():
        raise RuntimeError("nvidia-smi exploded")

    crashing = _monitor(boom)
    crashing._observe_sharing()  # must not raise
    assert crashing.sharing_metrics() == {}


def test_no_probe_means_the_feature_is_simply_off():
    m = JobMonitor("http://api.invalid", "m1", "secret", "job-2")
    m._observe_sharing()
    assert m.sharing_metrics() == {}


def test_the_probe_is_not_run_more_often_than_the_poll_interval():
    calls = []
    m = _monitor(lambda: calls.append(1) or [], poll=3600.0)
    for _ in range(5):
        m._observe_sharing()
    assert len(calls) == 1


def test_the_count_is_capped():
    m = _monitor(lambda: [f"p{i}" for i in range(500)])
    m._observe_sharing()
    assert m.sharing_metrics()["gpu_other_processes"] == 64


# ---- the runner attaches it to the result -----------------------------------------------------------------------
class _FakeMonitor:
    def __init__(self, shared):
        self._shared = shared
        self.env = {}
        self.stopped = False

    def set_output_dir(self, path):
        pass

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def sharing_metrics(self):
        return self._shared


def _spec():
    return JobSpec(job_id="job-9", workload_type="text-generation", model="m", input={}, execution_mode="hardware")


def _selector(result):
    class Ex:
        def run(self, spec):
            return result

    return lambda spec, caps=None: Ex()


def test_a_sighting_is_added_to_the_results_metrics():
    result = ExecutionResult(status="success", metrics={"duration_seconds": 2.0})
    out = jobrunner._run_open(_spec(), None, _selector(result), _FakeMonitor({"gpu_shared": True, "gpu_other_processes": 1}))
    assert out.metrics == {"duration_seconds": 2.0, "gpu_shared": True, "gpu_other_processes": 1}


def test_a_failed_job_carries_it_too():
    result = ExecutionResult(status="failed", metrics={"duration_seconds": 2.0}, error={"code": "oom", "message": "x"})
    out = jobrunner._run_open(_spec(), None, _selector(result), _FakeMonitor({"gpu_shared": True, "gpu_other_processes": 2}))
    assert out.metrics["gpu_shared"] is True


def test_nothing_is_added_when_nothing_was_seen_or_there_is_no_monitor():
    plain = ExecutionResult(status="success", metrics={"duration_seconds": 2.0})
    assert jobrunner._run_open(_spec(), None, _selector(plain), _FakeMonitor({})).metrics == {"duration_seconds": 2.0}
    plain2 = ExecutionResult(status="success", metrics={"duration_seconds": 2.0})
    assert jobrunner._run_open(_spec(), None, _selector(plain2), None).metrics == {"duration_seconds": 2.0}


def test_a_monitor_that_cannot_report_sharing_never_fails_the_job():
    class OldMonitor:  # no sharing_metrics at all
        env = {}

        def set_output_dir(self, path):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    class BrokenMonitor(OldMonitor):
        def sharing_metrics(self):
            raise RuntimeError("boom")

    for monitor in (OldMonitor(), BrokenMonitor()):
        result = ExecutionResult(status="success", metrics={"duration_seconds": 2.0})
        out = jobrunner._run_open(_spec(), None, _selector(result), monitor)
        assert out.status == "success" and out.metrics == {"duration_seconds": 2.0}


def test_the_monitor_is_still_stopped_and_the_signal_cleaned_up():
    from computecoin_agent import interrupts

    monitor = _FakeMonitor({"gpu_shared": True, "gpu_other_processes": 1})
    jobrunner._run_open(_spec(), None, _selector(ExecutionResult(status="success")), monitor)
    assert monitor.stopped is True
    assert interrupts.REGISTRY.running() == 0  # the host-interrupt registration did not leak


# ---- where the probe comes from ---------------------------------------------------------------------------------
def test_the_probe_exists_on_a_linux_host_and_not_elsewhere(monkeypatch):
    from computecoin_agent import platform as plat

    monkeypatch.setattr(plat, "get_platform", lambda: types.SimpleNamespace(name="linux"))
    assert callable(jobrunner._shared_gpu_probe())
    monkeypatch.setattr(plat, "get_platform", lambda: types.SimpleNamespace(name=plat.PLATFORM_WINDOWS))
    assert jobrunner._shared_gpu_probe() is None
