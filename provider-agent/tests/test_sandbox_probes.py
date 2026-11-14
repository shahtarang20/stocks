"""A host only offers a sandbox that Docker can actually use, and is told plainly what is missing."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from computecoin_agent import cli, isolation, watchdog
from computecoin_agent.config import AgentConfig
from computecoin_agent.isolation import (
    detect_supported_isolation_tiers,
    docker_runtimes,
    probe_gvisor,
    probe_microvm,
    sandbox_hint,
)

RUNC = frozenset({"runc"})
WITH_RUNSC = frozenset({"runc", "runsc"})
WITH_KATA = frozenset({"runc", "kata-qemu"})



@pytest.fixture(autouse=True)
def _no_background_watchdog_thread(monkeypatch):
    """_start_safeguards starts a thread that would, after 30 s, really start a watchdog process. Never in a test."""
    monkeypatch.setattr(watchdog, "keep_running", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _fresh_cache():
    isolation._forget_docker_runtimes()
    yield
    isolation._forget_docker_runtimes()


# ---- the probes --------------------------------------------------------------------------------------------------
def test_gvisor_needs_both_the_program_and_docker_knowing_about_it():
    assert probe_gvisor(runtimes=WITH_RUNSC, have_binary=True) is True
    assert probe_gvisor(runtimes=WITH_RUNSC, have_binary=False) is False  # registered but the program is gone
    assert probe_gvisor(runtimes=RUNC, have_binary=True) is False  # the program is there, Docker does not know it
    assert probe_gvisor(runtimes=frozenset(), have_binary=True) is False  # Docker could not be asked: fail closed


def test_a_microvm_needs_kata_registered_and_a_bare_firecracker_is_not_enough(monkeypatch):
    assert probe_microvm(runtimes=WITH_KATA, have_binary=True) is True
    assert probe_microvm(runtimes=RUNC, have_binary=True) is False
    assert probe_microvm(runtimes=WITH_KATA, have_binary=False) is False
    # firecracker alone used to count; the agent does not run jobs through it, so it must not be advertised
    monkeypatch.setattr(isolation, "which", lambda name: "/usr/bin/firecracker" if name == "firecracker" else None)
    assert probe_microvm(runtimes=WITH_KATA) is False


def test_the_tier_list_follows_the_probes():
    both = detect_supported_isolation_tiers(probe_gvisor_fn=lambda: True, probe_microvm_fn=lambda: True, probe_cc_fn=lambda: False)
    assert both == ["container", "gvisor", "microvm"]
    none = detect_supported_isolation_tiers(probe_gvisor_fn=lambda: False, probe_microvm_fn=lambda: False, probe_cc_fn=lambda: False)
    assert none == ["container"]


# ---- asking Docker -----------------------------------------------------------------------------------------------
def test_docker_runtimes_reads_the_names_from_docker_info():
    text = json.dumps({"runc": {"path": "runc"}, "runsc": {"path": "/usr/local/bin/runsc"}})
    assert docker_runtimes(lambda: text) == WITH_RUNSC


@pytest.mark.parametrize("answer", [None, "", "not json", "[]", "null", "42", '"runsc"'])
def test_an_unusable_answer_means_no_runtimes_never_a_guess(answer):
    assert docker_runtimes(lambda: answer) == frozenset()


def test_the_answer_is_kept_for_a_minute_and_then_asked_again(monkeypatch):
    calls, clock = [], [0.0]
    monkeypatch.setattr(isolation, "_ask_docker_runtimes", lambda: calls.append(1) or '{"runc": {}}')
    now = lambda: clock[0]  # noqa: E731
    assert docker_runtimes(now=now) == RUNC and docker_runtimes(now=now) == RUNC
    assert len(calls) == 1  # the heartbeat asks every pulse; Docker is not asked every time
    clock[0] += 61
    docker_runtimes(now=now)
    assert len(calls) == 2


def test_docker_missing_is_an_empty_answer_not_a_crash(monkeypatch):
    def nope(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(isolation.subprocess, "run", nope)
    assert isolation._ask_docker_runtimes() is None
    assert docker_runtimes() == frozenset()


# ---- what the host is told ---------------------------------------------------------------------------------------
def test_the_hint_says_exactly_what_is_missing():
    ready = sandbox_hint(runtimes=WITH_RUNSC, binary=True)
    assert "ready" in ready and "sandboxed jobs" in ready
    unregistered = sandbox_hint(runtimes=RUNC, binary=True)
    assert "installed but Docker does not know" in unregistered and "runsc install" in unregistered
    absent = sandbox_hint(runtimes=RUNC, binary=False)
    assert "not installed" in absent and "ordinary jobs only" in absent
    unknown = sandbox_hint(runtimes=frozenset(), binary=False)
    assert "ordinary jobs only" in unknown.lower()


def test_start_prints_the_hint(monkeypatch):
    from computecoin_agent import platform as plat

    monkeypatch.setattr(plat, "get_platform", lambda: SimpleNamespace(name=plat.PLATFORM_LINUX))
    monkeypatch.setattr(watchdog, "reap_orphans", lambda: watchdog.ReapResult())
    monkeypatch.setattr(watchdog, "ensure_running", lambda: "already_running")
    monkeypatch.setattr(isolation, "sandbox_hint", lambda: "Sandbox (gVisor) is not installed: test")
    said = []
    cli._start_safeguards(AgentConfig(), said.append)
    assert "[sandbox] Sandbox (gVisor) is not installed: test" in said


def test_a_failed_question_is_asked_again_soon_not_held_for_a_minute(monkeypatch):
    answers = iter([None, '{"runc": {}, "runsc": {}}'])
    calls, clock = [], [0.0]
    monkeypatch.setattr(isolation, "_ask_docker_runtimes", lambda: calls.append(1) or next(answers))
    now = lambda: clock[0]  # noqa: E731
    assert docker_runtimes(now=now) == frozenset()  # docker was busy for a moment
    clock[0] += 2
    assert docker_runtimes(now=now) == frozenset() and len(calls) == 1  # not hammered
    clock[0] += 4  # 6 s: the short failure memory is over
    assert docker_runtimes(now=now) == WITH_RUNSC and len(calls) == 2  # gVisor is back well inside a minute
    clock[0] += 30
    assert docker_runtimes(now=now) == WITH_RUNSC and len(calls) == 2  # a good answer is kept the full minute
