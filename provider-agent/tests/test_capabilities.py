"""W1: the agent's capability advertisement (capabilities.py) and its place in the heartbeat. WRITTEN, NOT YET RUN.

Pins: a flag appears only when a probe says so; a Windows host never reports the isolation / privacy / VM / multi-node
flags even if every probe claimed them (X5); the heartbeat health block carries the ``platform`` object and the list;
the legacy fields are unchanged.

Bug re-insertion: drop the Windows branch in compute_capabilities (the Windows tests fail); add a default flag (the
no-defaults test fails); stop adding the keys in heartbeat._add_platform_and_capabilities (the heartbeat tests fail).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from computecoin_agent import __version__ as agent_version
from computecoin_agent import capabilities as caps
from computecoin_agent import heartbeat
from computecoin_agent.platform import use_platform
from computecoin_agent.platform.fake import FakeGpu, FakeRuntime, make_fake_platform

KNOWN = caps.KNOWN_CAPABILITY_FLAGS


def gpu(vendor="nvidia", runtime=None):
    return SimpleNamespace(gpu_vendor=vendor, gpu_runtime=runtime or ("rocm" if vendor == "amd" else "cuda"), memory_total_mb=24576)


def report(gpus=(), tiers=("container",)):
    gpus = list(gpus)
    return SimpleNamespace(
        gpus=gpus, isolation_tiers=list(tiers),
        contract_summary=lambda: {"gpu_count": len(gpus), "vram_mb": sum(g.memory_total_mb for g in gpus)},
    )


def cfg(**kw):
    base = dict(gpu_power_limit_w=None, egress_proxy_image=None, cpu_only_kits_enabled=False, multinode_network=None,
                multinode_network_firewalled=False)
    base.update(kw)
    return SimpleNamespace(**base)


def compute(rep, plat, config=None, runtimes=(), vfio=None):
    return caps.compute_capabilities(rep, config=config or cfg(), plat=plat, isolation_runtimes_fn=lambda: list(runtimes), vfio_fn=lambda: vfio)


def test_flag_list_matches_the_plan():
    assert set(KNOWN) == {
        "container_tier", "nvidia_cuda", "amd_rocm", "gvisor", "microvm", "sealed", "sealed_pro", "confidential",
        "vfio_gpu", "multi_node", "egress_proxy", "thermal_cap", "power_limit", "multi_gpu", "cpu_capable",
    }
    assert caps.WINDOWS_FORBIDDEN_FLAGS <= set(KNOWN)


def test_no_flag_is_a_default():
    plat = make_fake_platform(docker_available=False)
    assert compute(report(), plat) == []  # no docker, no GPU, no opt-in: nothing is claimed
    assert compute(report([gpu()]), plat) == ["thermal_cap"]  # nvidia_cuda needs a container engine too


def test_cpu_only_linux_host_reports_just_the_container_tier():
    assert compute(report(), make_fake_platform()) == ["container_tier"]


def test_linux_nvidia_host_reports_what_it_detects_in_stable_order():
    plat = make_fake_platform()
    plat.gpu = FakeGpu(supported=True)
    out = compute(
        report([gpu(), gpu()], tiers=("container", "sealed", "sealed_pro", "confidential")), plat,
        config=cfg(gpu_power_limit_w=250.0, egress_proxy_image="r/p@sha256:" + "a" * 64, cpu_only_kits_enabled=True,
                   multinode_network="ccnet", multinode_network_firewalled=True),
        runtimes=["gvisor", "microvm"], vfio=True,
    )
    assert out == [f for f in KNOWN if f != "amd_rocm"]  # every flag but ROCm, in KNOWN order
    assert "amd_rocm" not in out


def test_amd_flag_needs_the_gpu_the_kfd_device_and_a_container_engine():
    rocm = report([gpu("amd")])
    assert "amd_rocm" in compute(rocm, make_fake_platform(kfd=True))
    assert "amd_rocm" not in compute(rocm, make_fake_platform(kfd=False))
    assert "amd_rocm" not in compute(rocm, make_fake_platform(kfd=True, docker_available=False))
    assert "nvidia_cuda" not in compute(rocm, make_fake_platform(kfd=True))


def test_multi_gpu_needs_two_gpus_of_one_vendor():
    assert "multi_gpu" not in compute(report([gpu()]), make_fake_platform())
    assert "multi_gpu" in compute(report([gpu(), gpu()]), make_fake_platform())
    assert "multi_gpu" not in compute(report([gpu(), gpu("amd")]), make_fake_platform(kfd=True))


def test_power_limit_needs_a_configured_limit_and_permission():
    plat = make_fake_platform()
    plat.gpu = FakeGpu(supported=True)
    assert "power_limit" in compute(report([gpu()]), plat, config=cfg(gpu_power_limit_w=200))
    assert "power_limit" not in compute(report([gpu()]), plat, config=cfg(gpu_power_limit_w=None))
    assert "power_limit" not in compute(report([gpu()]), plat, config=cfg(gpu_power_limit_w=0))
    plat.gpu = FakeGpu(supported=False)
    assert "power_limit" not in compute(report([gpu()]), plat, config=cfg(gpu_power_limit_w=200))


def test_private_flags_come_only_from_the_detected_tiers():
    assert "sealed" in compute(report(tiers=("container", "sealed")), make_fake_platform())
    assert "sealed_pro" not in compute(report(tiers=("container", "sealed")), make_fake_platform())
    assert not {"sealed", "sealed_pro", "confidential"} & set(compute(report(tiers=("container", "open")), make_fake_platform()))


def test_multi_node_needs_the_network_and_the_firewall_attestation():
    assert "multi_node" not in compute(report(), make_fake_platform(), config=cfg(multinode_network="ccnet"))
    assert "multi_node" in compute(report(), make_fake_platform(), config=cfg(multinode_network="ccnet", multinode_network_firewalled=True))


# ---- X5: Windows never reports the forbidden flags -----------------------------------------------------------------
def test_windows_host_never_reports_isolation_privacy_vm_or_multinode_flags():
    plat = make_fake_platform(name="windows")
    plat.gpu = FakeGpu(supported=True)
    out = compute(
        report([gpu(), gpu()], tiers=("container", "gvisor", "microvm", "sealed", "sealed_pro", "confidential")), plat,
        config=cfg(gpu_power_limit_w=250, egress_proxy_image="r/p@sha256:" + "a" * 64, cpu_only_kits_enabled=True,
                   multinode_network="ccnet", multinode_network_firewalled=True),
        runtimes=["gvisor", "microvm"], vfio=True,
    )
    assert not set(out) & caps.WINDOWS_FORBIDDEN_FLAGS
    assert out == ["container_tier", "nvidia_cuda", "egress_proxy", "thermal_cap", "power_limit", "multi_gpu", "cpu_capable"]


def test_windows_amd_is_not_advertised_without_a_kfd_device():
    assert "amd_rocm" not in compute(report([gpu("amd")]), make_fake_platform(name="windows", kfd=False))


# ---- failures fail closed -----------------------------------------------------------------------------------------------
def test_probe_exceptions_drop_the_flag_instead_of_raising():
    plat = make_fake_platform()

    class Boom(FakeRuntime):
        def engine_available(self):
            raise RuntimeError("docker probe exploded")

    plat.runtime = Boom()
    assert compute(report([gpu()]), plat) == ["thermal_cap"]


# ---- the heartbeat -----------------------------------------------------------------------------------------------------
@pytest.fixture
def quiet_probes(monkeypatch):
    monkeypatch.setattr(heartbeat, "isolation_runtimes", lambda: [])
    monkeypatch.setattr(heartbeat, "_vfio_gpu_flag", lambda: None)
    monkeypatch.setattr(heartbeat, "_cpu_capable_flag", lambda: None)
    monkeypatch.setattr(caps, "_load_config", lambda: cfg())


def test_heartbeat_health_carries_platform_and_capabilities(quiet_probes):
    plat = make_fake_platform(supervisor_version="2.0.1")
    with use_platform(plat):
        health = heartbeat.default_health(report([gpu()]))
    assert health["platform"] == {"os": "linux", "os_version": plat.os_version(), "runtime_backend": "linux_docker",
                                  "agent_version": agent_version, "supervisor_version": "2.0.1"}
    assert health["capabilities"] == ["container_tier", "nvidia_cuda", "thermal_cap"]
    # the legacy fields are untouched
    assert {"gpu_temp_c", "gpu_util_pct", "mem_used_mb", "uptime_s", "isolation_runtimes"} <= set(health)
    assert "vfio_gpu" not in health and "cpu_capable" not in health


def test_contract_body_shape_is_unchanged_apart_from_the_additive_keys(quiet_probes):
    with use_platform(make_fake_platform()):
        body = heartbeat.build_contract_heartbeat_body(report([gpu()]))
    assert set(body) == {"availability", "capability_summary", "health"}
    assert set(body["health"]) - {"gpu_temp_c", "gpu_util_pct", "mem_used_mb", "uptime_s", "isolation_runtimes"} == {"platform", "capabilities"}


def test_windows_heartbeat_reports_no_isolation_runtimes_and_no_vfio(monkeypatch):
    monkeypatch.setattr(heartbeat, "isolation_runtimes", lambda: ["gvisor", "microvm"])
    monkeypatch.setattr(heartbeat, "_vfio_gpu_flag", lambda: True)
    monkeypatch.setattr(heartbeat, "_cpu_capable_flag", lambda: None)
    monkeypatch.setattr(caps, "_load_config", lambda: cfg())
    with use_platform(make_fake_platform(name="windows")):
        health = heartbeat.default_health(report([gpu()], tiers=("container", "sealed", "confidential")))
    assert health["platform"]["os"] == "windows" and health["platform"]["runtime_backend"] == "wsl_engine"
    assert health["isolation_runtimes"] == []
    assert "vfio_gpu" not in health
    assert not set(health["capabilities"]) & caps.WINDOWS_FORBIDDEN_FLAGS


def test_capabilities_key_is_absent_when_flags_cannot_be_computed(monkeypatch):
    monkeypatch.setattr(heartbeat, "isolation_runtimes", lambda: [])
    monkeypatch.setattr(heartbeat, "_vfio_gpu_flag", lambda: None)
    monkeypatch.setattr(heartbeat, "_cpu_capable_flag", lambda: None)

    def boom(*a, **k):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(caps, "compute_capabilities", boom)
    with use_platform(make_fake_platform()):
        health = heartbeat.default_health(report())
    assert "capabilities" not in health  # absent means "nothing proven": the scheduler fails closed on it
