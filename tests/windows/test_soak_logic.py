"""Logic of the soak driver, with a fake clock. Runs everywhere (no GPU). WRITTEN, NOT RUN."""

from __future__ import annotations

import json

import pytest

import soak


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _run(hours=2.0, probe=None, inject=None, state=None, poll=60.0):
    c = Clock()
    res = soak.run_soak(hours, probe or (lambda: {"service": "running", "distro_ok": True, "gpu_ok": True}),
                        inject if inject is not None else {k: (lambda: None) for k in soak.EVENTS},
                        poll_seconds=poll, state_path=state, clock=c.now, sleep=c.sleep)
    return res, c


def test_healthy_run_with_every_event_injected_passes():
    calls = []
    inject = {k: (lambda k=k: calls.append(k)) for k in soak.EVENTS}
    res, _ = _run(inject=inject)
    assert res.passed() and res.polls >= 100
    assert sorted(set(calls)) == sorted(soak.EVENTS) and len(calls) == 9


def test_a_missing_injector_is_incomplete_not_passed():
    res, _ = _run(inject={"yield": lambda: None, "sleep": lambda: None, "reboot": None})
    assert res.skipped and not res.passed() and res.passed(allow_skipped=True)


def test_one_unhealthy_poll_fails_the_soak():
    n = {"i": 0}

    def probe():
        n["i"] += 1
        return {"service": "running", "distro_ok": n["i"] != 50, "gpu_ok": True}

    res, _ = _run(probe=probe)
    assert len(res.unhealthy) == 1 and not res.passed()


def test_missing_fields_and_probe_errors_are_unhealthy():
    assert not soak.is_healthy({})
    assert not soak.is_healthy({"service": "running", "distro_ok": True})
    assert not soak.is_healthy({"service": "stopped", "distro_ok": True, "gpu_ok": True})

    def boom():
        raise ValueError("x")

    res, _ = _run(probe=boom)
    assert not res.passed() and res.unhealthy[0]["sample"] == {"error": "ValueError"}


def test_schedule_is_inside_the_run_and_round_robin():
    plan = soak.schedule(24)
    assert [e for _, e in plan[:3]] == list(soak.EVENTS)
    assert all(0.1 * 86400 <= t <= 0.9 * 86400 for t, _ in plan)
    assert [t for t, _ in plan] == sorted(t for t, _ in plan)


def test_resume_after_a_reboot_continues_from_the_saved_state(tmp_path):
    state = tmp_path / "state.json"
    c = Clock()
    seen: list[str] = []

    def reboot():
        seen.append("reboot")
        raise SystemExit("machine went down")  # the process ends here, as it would on a real reboot

    inject = {"yield": lambda: seen.append("yield"), "sleep": lambda: seen.append("sleep"), "reboot": reboot}
    with pytest.raises(SystemExit):
        soak.run_soak(2.0, lambda: {"service": "running", "distro_ok": True, "gpu_ok": True}, inject, poll_seconds=60,
                      state_path=state, clock=c.now, sleep=c.sleep)
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["next_event"] == 3 and "\r" not in state.read_text(encoding="utf-8")
    # A new process starts: same state file, a clock that continues. The reboot is not repeated.
    seen.clear()
    inject["reboot"] = lambda: seen.append("reboot")
    res = soak.run_soak(2.0, lambda: {"service": "running", "distro_ok": True, "gpu_ok": True}, inject, poll_seconds=60,
                        state_path=state, clock=c.now, sleep=c.sleep)
    assert seen.count("reboot") == 2 and res.passed()  # events 5 and 8 only; events 0..2 were not replayed
