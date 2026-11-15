"""The network-filter image is built in: a host needs no setting, can replace it, and can turn it off."""

from __future__ import annotations

import pytest

from computecoin_agent import config as cfgmod
from computecoin_agent.config import DEFAULT_EGRESS_PROXY_IMAGE, AgentConfig


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("COMPUTECOIN_EGRESS_PROXY_IMAGE", raising=False)


def test_the_built_in_image_is_the_projects_own_pinned_by_digest():
    assert DEFAULT_EGRESS_PROXY_IMAGE.startswith("ghcr.io/") and "/computecoin-egress-proxy@sha256:" in DEFAULT_EGRESS_PROXY_IMAGE
    digest = DEFAULT_EGRESS_PROXY_IMAGE.split("@sha256:", 1)[1]
    assert len(digest) == 64 and digest == digest.lower() and all(c in "0123456789abcdef" for c in digest)
    assert ":latest" not in DEFAULT_EGRESS_PROXY_IMAGE  # never a moving tag


def test_a_host_with_no_setting_uses_it():
    assert AgentConfig().egress_proxy_image == DEFAULT_EGRESS_PROXY_IMAGE


def test_a_host_can_replace_it_by_file_value_or_environment(monkeypatch):
    mine = "registry.example/proxy@sha256:" + "a" * 64
    assert AgentConfig(egress_proxy_image=mine).egress_proxy_image == mine
    monkeypatch.setenv("COMPUTECOIN_EGRESS_PROXY_IMAGE", "registry.example/other@sha256:" + "b" * 64)
    assert AgentConfig().egress_proxy_image == "registry.example/other@sha256:" + "b" * 64


@pytest.mark.parametrize("off", ["", "   "])
def test_an_empty_setting_turns_it_off(off, monkeypatch):
    assert AgentConfig(egress_proxy_image=off).egress_proxy_image is None
    monkeypatch.setenv("COMPUTECOIN_EGRESS_PROXY_IMAGE", off)
    assert AgentConfig().egress_proxy_image is None


@pytest.mark.parametrize("bad", ["ghcr.io/x/proxy:latest", "ghcr.io/x/proxy", "proxy"])
def test_an_unpinned_replacement_is_refused(bad):
    with pytest.raises(ValueError):
        AgentConfig(egress_proxy_image=bad)


def test_the_host_reports_the_filter_flag_by_default_and_not_when_turned_off():
    from test_capabilities import cfg, compute, report
    from computecoin_agent.platform.fake import make_fake_platform

    plat = make_fake_platform(docker_available=True)
    on = compute(report(), plat, config=cfg(egress_proxy_image=AgentConfig().egress_proxy_image))
    off = compute(report(), plat, config=cfg(egress_proxy_image=AgentConfig(egress_proxy_image="").egress_proxy_image))
    assert "egress_proxy" in on and "egress_proxy" not in off
