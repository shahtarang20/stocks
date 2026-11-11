"""The agent declares what kind of machine it is (dedicated server or shared PC) in its heartbeat."""

from __future__ import annotations

import types

import pytest

from computecoin_agent import heartbeat
from computecoin_agent.config import AgentConfig


def test_config_accepts_only_dedicated_or_shared():
    assert AgentConfig(host_kind=" Dedicated ").host_kind == "dedicated"
    assert AgentConfig(host_kind="shared").host_kind == "shared"
    assert AgentConfig().host_kind is None
    with pytest.raises(ValueError):
        AgentConfig(host_kind="server-ish")


def _patch(monkeypatch, *, platform_name, configured):
    from computecoin_agent import platform as plat_mod

    monkeypatch.setattr(plat_mod, "get_platform", lambda: types.SimpleNamespace(name=platform_name))
    monkeypatch.setattr(AgentConfig, "load", classmethod(lambda cls, *a, **k: types.SimpleNamespace(host_kind=configured)))


def test_linux_host_reports_what_it_declared(monkeypatch):
    _patch(monkeypatch, platform_name="linux", configured="dedicated")
    assert heartbeat._host_kind_flag() == "dedicated"
    _patch(monkeypatch, platform_name="linux", configured="shared")
    assert heartbeat._host_kind_flag() == "shared"


def test_undeclared_host_reports_nothing(monkeypatch):
    _patch(monkeypatch, platform_name="linux", configured=None)
    assert heartbeat._host_kind_flag() is None


def test_windows_host_is_always_shared_even_if_it_claims_dedicated(monkeypatch):
    from computecoin_agent.platform import PLATFORM_WINDOWS

    _patch(monkeypatch, platform_name=PLATFORM_WINDOWS, configured="dedicated")
    assert heartbeat._host_kind_flag() == "shared"


def test_health_block_carries_host_kind_only_when_declared(monkeypatch):
    monkeypatch.setattr(heartbeat, "_host_kind_flag", lambda: "dedicated")
    assert heartbeat.default_health()["host_kind"] == "dedicated"
    monkeypatch.setattr(heartbeat, "_host_kind_flag", lambda: None)
    assert "host_kind" not in heartbeat.default_health()
