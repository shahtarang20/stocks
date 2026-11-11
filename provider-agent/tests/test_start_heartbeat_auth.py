"""`computecoin-agent start`: a rejected credential must stop the agent, not silently kill only the heartbeat."""

from __future__ import annotations

import threading

from computecoin_agent import cli
from computecoin_agent.heartbeat import HeartbeatAuthError


def test_heartbeat_auth_failure_flags_and_interrupts_the_main_thread(monkeypatch):
    def rejected(*a, **k):
        raise HeartbeatAuthError("heartbeat rejected: 401")

    monkeypatch.setattr(cli, "heartbeat_loop", rejected)
    flag, interrupted = threading.Event(), []

    cli._heartbeat_until_auth_failure(flag, "http://x", "m", "s", interrupt=lambda: interrupted.append(True))

    assert flag.is_set()
    assert interrupted == [True]


def test_a_healthy_heartbeat_loop_does_not_interrupt(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "heartbeat_loop", lambda *a, **k: calls.append((a, k)))
    flag, interrupted = threading.Event(), []

    cli._heartbeat_until_auth_failure(flag, "http://x", "m", "s", iterations=1, interrupt=lambda: interrupted.append(True))

    assert not flag.is_set() and interrupted == []
    assert calls and calls[0][0] == ("http://x", "m", "s") and calls[0][1] == {"iterations": 1}


def test_other_errors_are_not_swallowed_as_auth_failures(monkeypatch):
    import pytest

    def boom(*a, **k):
        raise RuntimeError("something else")

    monkeypatch.setattr(cli, "heartbeat_loop", boom)
    flag = threading.Event()
    with pytest.raises(RuntimeError):
        cli._heartbeat_until_auth_failure(flag, "http://x", "m", "s", interrupt=lambda: None)
    assert not flag.is_set()
