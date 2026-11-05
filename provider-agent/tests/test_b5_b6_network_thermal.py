"""B5 egress argv and B6 thermal guard (not yet executed)."""

from types import SimpleNamespace

import pytest

from computecoin_agent.executor import DockerExecutor, ExecutorError, JobSpec
from computecoin_agent.thermal import HARD_STOP, OK, THROTTLED, ThermalGuard

DIGEST = "img@sha256:" + "a" * 64
PROXY = "proxy@sha256:" + "b" * 64


def _spec(**kw):
    base = dict(job_id="j1", workload_type="text-generation", model="m", input={}, execution_mode="hardware", image_digest=DIGEST)
    base.update(kw)
    return JobSpec(**base)


def _ex():
    return DockerExecutor(docker_available=lambda: True, egress_proxy_image=PROXY)


def test_default_has_no_network():
    argv = _ex().build_argv(_spec(), "/tmp/out")
    assert argv[argv.index("--network") + 1] == "none"
    assert not any(a.startswith("HTTPS_PROXY") for a in argv)


def test_egress_uses_internal_net_and_proxy_env():
    argv = _ex().build_argv(_spec(network_policy={"egress": ["example.com:443"]}), "/tmp/out")
    assert argv[argv.index("--network") + 1] == "ccoin-net-j1"
    assert "HTTPS_PROXY=http://ccoin-proxy-j1:3128" in argv


def test_privacy_tier_refused():
    with pytest.raises(ExecutorError):
        _ex().build_argv(_spec(isolation_tier="sealed", network_policy={"egress": ["example.com:443"]}), "/tmp/out")


def _guard(temp):
    return ThermalGuard(85, 80, 95, sampler=lambda: [SimpleNamespace(temp_c=temp[0])])


def test_hysteresis_and_none():
    t = [70.0]
    g = _guard(t)
    assert g.check() == OK
    t[0] = 85.0
    assert g.check() == THROTTLED
    t[0] = 82.0
    assert g.check() == THROTTLED  # still above resume
    t[0] = 79.0
    assert g.check() == OK
    t[0] = 95.0
    assert g.check() == HARD_STOP
    t[0] = None
    assert g.check() == OK and g.availability() == "online"
