"""The host's owner needs the computer back: the running job is stopped cleanly and reported ``host_interrupted``.

The platform re-runs such a job on another computer for free and never charges the customer for the stopped attempt.
"""

from __future__ import annotations

import threading
import types

import pytest

from computecoin_agent import interrupts
from computecoin_agent.control_channel import (
    ControlMessage,
    ProtocolError,
    RunnerControl,
    validate_data,
)
from computecoin_agent.executor import DockerExecutor, DockerRunResult, JobSpec

DIGEST = "ghcr.io/x/y@sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _clean_registry():
    interrupts.REGISTRY.__init__()  # a fresh registry for every test
    yield
    interrupts.REGISTRY.__init__()


# ---- the registry -------------------------------------------------------------------------------------------------
def test_interrupt_all_sets_every_running_jobs_event_and_counts_them():
    a, b = interrupts.REGISTRY.register("a"), interrupts.REGISTRY.register("b")
    assert not a.is_set() and not b.is_set()
    assert interrupts.REGISTRY.interrupt_all("owner_active") == 2
    assert a.is_set() and b.is_set()
    assert interrupts.REGISTRY.reason() == "owner_active"


def test_with_nothing_running_it_is_a_harmless_no_op():
    assert interrupts.REGISTRY.interrupt_all("owner_active") == 0
    assert interrupts.REGISTRY.reason() is None
    # ...and it does not poison the next job: one that starts later is NOT stopped.
    assert not interrupts.REGISTRY.register("later").is_set()


def test_a_job_that_starts_in_the_middle_of_an_interruption_is_stopped_at_once():
    first = interrupts.REGISTRY.register("first")
    interrupts.REGISTRY.interrupt_all("owner_active")
    straggler = interrupts.REGISTRY.register("second")
    assert first.is_set() and straggler.is_set()


def test_the_interruption_ends_when_the_last_job_is_gone():
    interrupts.REGISTRY.register("a")
    interrupts.REGISTRY.interrupt_all("owner_active")
    interrupts.REGISTRY.unregister("a")
    assert interrupts.REGISTRY.reason() is None and interrupts.REGISTRY.running() == 0
    assert not interrupts.REGISTRY.register("fresh").is_set()


# ---- the executor: a real run that gets interrupted ---------------------------------------------------------------
def _executor(runner):
    ex = DockerExecutor(docker_available=lambda: True, nvidia_available=lambda: True)
    ex._runner = runner
    return ex


def _spec(**kw):
    return JobSpec(job_id="j-int", workload_type="kit", model="m", input={}, execution_mode="hardware",
                   image_digest=DIGEST, limits={"gpu_count": 0}, **kw)


def test_an_interrupted_job_stops_its_container_and_is_reported_host_interrupted(tmp_path):
    killed = threading.Event()
    calls: list[list[str]] = []

    def runner(argv, out):
        calls.append(list(argv))
        if argv[:2] == ["docker", "kill"]:
            killed.set()
            return DockerRunResult(0)
        if argv[:2] == ["docker", "rm"]:
            return DockerRunResult(0)
        # the container itself: runs until it is killed (exit 137), like a real long job
        assert killed.wait(10), "the container was never killed"
        return DockerRunResult(137)

    spec = _spec(output_dir=str(tmp_path))
    spec.interrupt_event = interrupts.REGISTRY.register(spec.job_id)
    threading.Timer(0.3, lambda: interrupts.REGISTRY.interrupt_all("owner_active")).start()

    result = _executor(runner).run(spec)

    assert result.status == "failed"
    assert result.error["code"] == "host_interrupted"
    assert ["docker", "kill", "ccoin-job-j-int"] in calls  # exactly this container, nothing else
    assert any("interrupted" in line for line in result.logs)


def test_a_job_that_is_not_interrupted_is_unaffected(tmp_path):
    spec = _spec(output_dir=str(tmp_path))
    spec.interrupt_event = interrupts.REGISTRY.register(spec.job_id)  # registered, never fired
    seen = []

    def runner(argv, out):
        seen.append(list(argv))
        return DockerRunResult(1, stderr="boom")  # an ordinary failure of the customer's own code

    result = _executor(runner).run(spec)
    assert result.status == "failed"
    assert result.error["code"] != "host_interrupted"
    assert not any(a[:2] == ["docker", "kill"] for a in seen)


def test_a_customer_cancel_still_wins_over_an_interruption(tmp_path):
    spec = _spec(output_dir=str(tmp_path))
    spec.cancel_event = threading.Event()
    spec.interrupt_event = interrupts.REGISTRY.register(spec.job_id)
    killed = threading.Event()

    def runner(argv, out):
        if argv[:2] == ["docker", "kill"]:
            killed.set()
            return DockerRunResult(0)
        if argv[:2] == ["docker", "rm"]:
            return DockerRunResult(0)
        killed.wait(10)
        return DockerRunResult(137)

    spec.cancel_event.set()  # the customer cancelled first
    result = _executor(runner).run(spec)
    assert result.error["code"] == "cancelled"  # the customer's wish is what is reported


# ---- the control channel -------------------------------------------------------------------------------------------
def test_interrupt_is_a_valid_supervisor_message_with_an_optional_reason():
    assert validate_data("interrupt", {}) == {}
    assert validate_data("interrupt", {"reason": "fullscreen"}) == {"reason": "fullscreen"}
    with pytest.raises(ProtocolError):
        validate_data("interrupt", {"reason": "x" * 201})
    with pytest.raises(ProtocolError):
        validate_data("interrupt", {"unexpected": 1})


def test_the_runner_obeys_an_interrupt_by_stopping_the_running_jobs():
    ctl = RunnerControl()
    job_event = interrupts.REGISTRY.register("job-1")
    ctl.handle(ControlMessage(type="interrupt", data={"reason": "foreground_gpu_app"}, seq=1))
    assert job_event.is_set()
    assert interrupts.REGISTRY.reason() == "foreground_gpu_app"


# ---- the supervisor: ask politely first, kill only if that fails --------------------------------------------------
class _Timer:
    """A timer that fires only when the test says so."""

    instances: list["_Timer"] = []

    def __init__(self, interval, fn):
        self.interval, self.fn, self.started, self.daemon = interval, fn, False, False
        _Timer.instances.append(self)

    def start(self):
        self.started = True

    def fire(self):
        self.fn()


def _supervisor():
    from test_windows_supervisor import make

    _Timer.instances = []
    sup = make(timer_factory=_Timer)
    sup._runner_active_jobs = 1
    sup._proc = types.SimpleNamespace(
        terminated=False, killed=False, code=None,
        poll=lambda: None, terminate=lambda: None, kill=lambda: None, wait=lambda timeout=None: None,
    )
    sent: list[tuple[str, dict]] = []
    sup._send_quiet = lambda t, d: (sent.append((t, d)) or True)
    return sup, sent


def test_a_governance_stop_asks_the_runner_to_interrupt_before_anything_is_killed():
    sup, sent = _supervisor()
    terminated = []
    sup._terminate_process = lambda: terminated.append(True)
    sup.abort_running_job("fullscreen")
    assert sent == [("interrupt", {"reason": "fullscreen"})]
    assert terminated == []  # not killed yet: the runner was asked first
    assert len(_Timer.instances) == 1 and _Timer.instances[0].started
    assert _Timer.instances[0].interval == sup._s.interrupt_grace_seconds


def test_if_the_job_has_not_stopped_when_the_timer_fires_the_runner_is_terminated():
    sup, _sent = _supervisor()
    terminated = []
    sup._terminate_process = lambda: terminated.append(True)
    sup.abort_running_job("fullscreen")
    _Timer.instances[0].fire()  # the job is still reported running
    assert terminated == [True]


def test_if_the_job_stopped_by_itself_nothing_is_killed():
    sup, _sent = _supervisor()
    terminated = []
    sup._terminate_process = lambda: terminated.append(True)
    sup.abort_running_job("fullscreen")
    sup._runner_active_jobs = 0  # the runner stopped the container and reported it
    _Timer.instances[0].fire()
    assert terminated == []


def test_only_one_fallback_timer_is_pending_at_a_time():
    sup, sent = _supervisor()
    sup.abort_running_job("fullscreen")
    sup.abort_running_job("fullscreen")
    assert len(_Timer.instances) == 1
    _Timer.instances[0].fire()
    sup.abort_running_job("again")  # after the timer is spent, a new stop gets a new fallback
    assert len(_Timer.instances) == 2


def test_if_the_runner_cannot_be_reached_it_is_terminated_at_once_as_before():
    sup, _sent = _supervisor()
    terminated = []
    sup._terminate_process = lambda: terminated.append(True)
    sup._send_quiet = lambda t, d: False  # nobody to ask
    sup.abort_running_job("fullscreen")
    assert terminated == [True] and _Timer.instances == []


def test_nothing_happens_when_no_job_is_running():
    sup, sent = _supervisor()
    sup._runner_active_jobs = 0
    sup.abort_running_job("fullscreen")
    assert sent == [] and _Timer.instances == []
