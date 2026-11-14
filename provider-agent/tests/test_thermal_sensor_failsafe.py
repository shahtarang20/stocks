"""A GPU temperature sensor that worked and then went quiet must not leave a hot card unwatched."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from computecoin_agent.config import AgentConfig
from computecoin_agent.executor import DockerExecutor, DockerRunResult, JobSpec
from computecoin_agent.thermal import HARD_STOP, OK, SENSOR_LOST, THROTTLED, ThermalGuard

DIGEST = "ghcr.io/x/y@sha256:" + "a" * 64


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _guard(readings, clock=None, lost=120.0):
    """readings: a one-item list holding the current temperature (None = unreadable)."""
    return ThermalGuard(
        85, 80, 95,
        sampler=lambda: [SimpleNamespace(temp_c=readings[0])] if readings[0] is not None else [],
        sensor_lost_seconds=lost,
        clock=clock or Clock(),
    )


# ---- the guard --------------------------------------------------------------------------------------------------
def test_a_computer_that_never_had_a_sensor_is_left_alone():
    clock, t = Clock(), [None]
    g = _guard(t, clock)
    for _ in range(5):
        clock.now += 1000
        assert g.check() == OK
    assert g.has_sensor() is False and g.availability() == "online"


def test_a_short_gap_keeps_the_previous_state_instead_of_clearing_it():
    clock, t = Clock(), [90.0]
    g = _guard(t, clock)
    assert g.check() == THROTTLED  # hot: throttled
    t[0] = None
    clock.now += 30  # a blip, well under the limit
    assert g.check() == THROTTLED  # still throttled: an unreadable sensor is not "cooled down"
    t[0] = 60.0
    assert g.check() == OK  # a real reading ends it at once


def test_a_short_gap_while_cool_stays_online():
    clock, t = Clock(), [60.0]
    g = _guard(t, clock)
    assert g.check() == OK
    t[0] = None
    clock.now += 60
    assert g.check() == OK and g.availability() == "online"


def test_a_sensor_quiet_for_too_long_is_lost_and_blocks_new_work():
    clock, t = Clock(), [60.0]
    g = _guard(t, clock, lost=120)
    assert g.check() == OK
    t[0] = None
    assert g.check() == OK  # the clock starts at the first missed reading
    clock.now += 119
    assert g.check() == OK
    clock.now += 1
    assert g.check() == SENSOR_LOST
    assert g.availability() == "throttled" and g.may_claim() is False
    assert g.blind_seconds() == pytest.approx(120)


def test_any_real_reading_ends_the_lost_state():
    clock, t = Clock(), [60.0]
    g = _guard(t, clock, lost=10)
    g.check()
    t[0] = None
    g.check()
    clock.now += 50
    assert g.check() == SENSOR_LOST
    t[0] = 62.0
    assert g.check() == OK and g.blind_seconds() == 0.0 and g.may_claim() is True


def test_a_gap_is_measured_from_the_first_missed_reading_not_from_each_one():
    clock, t = Clock(), [60.0]
    g = _guard(t, clock, lost=100)
    g.check()
    t[0] = None
    for _ in range(9):  # misses at 0, 10 ... 80 s
        g.check()
        clock.now += 10
    assert g.check() == OK  # 90 s since the first miss: still within the limit
    clock.now += 10
    assert g.check() == SENSOR_LOST  # 100 s since the FIRST miss, however many misses there were in between
    t[0] = 61.0
    g.check()
    t[0] = None
    assert g.check() == OK  # a fresh gap starts a fresh clock


def test_the_same_number_for_hours_is_not_a_fault():
    clock, t = Clock(), [38.0]
    g = _guard(t, clock, lost=10)
    for _ in range(50):
        clock.now += 600
        assert g.check() == OK  # whole-degree sensors repeat themselves


def test_the_hard_stop_still_wins_and_a_sensor_that_stays_hot_is_unchanged():
    clock, t = Clock(), [96.0]
    g = _guard(t, clock)
    assert g.check() == HARD_STOP


def test_the_guard_refuses_a_nonsense_limit():
    with pytest.raises(ValueError):
        ThermalGuard(85, 80, 95, sensor_lost_seconds=0)


def test_it_is_set_from_the_config_and_bad_values_are_refused():
    assert ThermalGuard.from_config(AgentConfig(gpu_sensor_lost_seconds=45)).sensor_lost_seconds == 45
    assert ThermalGuard.from_config(AgentConfig()).sensor_lost_seconds == 120
    for bad in (0, 5, 99999, -1):
        with pytest.raises(ValueError):
            AgentConfig(gpu_sensor_lost_seconds=bad)


# ---- the executor: a running job is stopped, and it is the host's doing -------------------------------------------
def _executor(runner, guard):
    ex = DockerExecutor(docker_available=lambda: True, nvidia_available=lambda: True, thermal_guard=guard, thermal_poll_seconds=0.02)
    ex._runner = runner
    return ex


def _spec(tmp_path, job_id="j-blind"):
    return JobSpec(job_id=job_id, workload_type="kit", model="m", input={}, execution_mode="hardware",
                   image_digest=DIGEST, limits={"gpu_count": 0}, output_dir=str(tmp_path))


def _long_runner(killed, calls):
    def runner(argv, out):
        calls.append(list(argv))
        if argv[:2] == ["docker", "kill"]:
            killed.set()
            return DockerRunResult(0)
        if argv[:2] == ["docker", "rm"]:
            return DockerRunResult(0)
        assert killed.wait(10), "the container was never killed"
        return DockerRunResult(137)

    return runner


def test_a_job_is_stopped_when_the_sensor_goes_quiet_and_reported_as_a_thermal_stop(tmp_path):
    clock, t = Clock(), [60.0]
    g = _guard(t, clock, lost=50)
    g.check()  # the sensor has worked
    t[0] = None
    g.check()
    clock.now += 500  # ...and has been quiet far longer than allowed
    killed, calls = threading.Event(), []
    result = _executor(_long_runner(killed, calls), g).run(_spec(tmp_path))
    assert result.status == "failed"
    assert result.error["code"] == "thermal_shutdown"  # the code the platform treats as the host's doing: never charged
    assert ["docker", "kill", "ccoin-job-j-blind"] in calls
    assert any("unreadable" in line for line in result.logs)


def test_a_working_sensor_never_stops_a_job(tmp_path):
    t = [60.0]
    g = _guard(t)
    seen = []

    def runner(argv, out):
        seen.append(list(argv))
        return DockerRunResult(0, stdout="ok")

    result = _executor(runner, g).run(_spec(tmp_path, "j-ok"))
    assert not any(a[:2] == ["docker", "kill"] for a in seen)
    assert (result.error or {}).get("code") != "thermal_shutdown"


def test_a_host_with_no_sensor_at_all_runs_jobs_as_before(tmp_path):
    clock, t = Clock(), [None]
    g = _guard(t, clock, lost=10)
    clock.now += 10_000
    seen = []

    def runner(argv, out):
        seen.append(list(argv))
        return DockerRunResult(0, stdout="ok")

    _executor(runner, g).run(_spec(tmp_path, "j-nosensor"))
    assert not any(a[:2] == ["docker", "kill"] for a in seen)


def test_a_failing_check_does_not_stop_a_job(tmp_path):
    class Exploding:
        def read_max_temp(self):
            raise RuntimeError("driver exploded")

        def evaluate(self, temp):  # pragma: no cover - never reached
            raise AssertionError

        def blind_seconds(self):  # pragma: no cover
            return 0.0

    seen = []

    def runner(argv, out):
        seen.append(list(argv))
        return DockerRunResult(0, stdout="ok")

    _executor(runner, Exploding()).run(_spec(tmp_path, "j-boom"))
    assert not any(a[:2] == ["docker", "kill"] for a in seen)
