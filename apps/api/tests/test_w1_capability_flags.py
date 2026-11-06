"""W1 (cross-platform plan): capability flags in the heartbeat, and the scheduler gates built on them. WRITTEN, NOT RUN.

Pure unit tests (SimpleNamespace machines and jobs, no database). They pin:
  * the heartbeat schema keeps only known flags and known platform keys, size-capped (W1.1);
  * a host that reports flags is refused work that needs a flag it lacks, and a host with no ``capabilities`` gets
    nothing (W1.2, fail closed);
  * legacy agents (no platform object) behave exactly as before for plain container jobs (no new refusal);
  * a Windows host NEVER receives private-tier, BYOC, gVisor, microVM, Windows-VM or multi-node work, even when it claims
    the flags (W1.3, X5);
  * the admin machine view exposes the sanitised platform and flags.

Bug re-insertion (each removal makes a named test fail): remove the WINDOWS check in capability_refusal ->
test_windows_host_never_gets_private_byoc_isolation_vm_or_multinode; return None when capabilities are absent ->
test_flag_host_without_capabilities_gets_nothing; drop the known-flag filter in sanitize_capabilities ->
test_heartbeat_health_drops_unknown_flags_and_caps_size.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.provider import HeartbeatHealth, MachineOut
from app.services import capability_flags as cf
from app.services.gpu_runtime import is_gpu_compatible
from app.services.scheduler import _machine_supports_tier_and_policy
from app.services.scheduler_v2 import machine_fit_reason

LINUX_PLATFORM = {"os": "linux", "os_version": "6.8.0", "runtime_backend": "linux_docker", "agent_version": "0.1.0"}
WINDOWS_PLATFORM = {"os": "windows", "os_version": "10.0.22631", "runtime_backend": "wsl_engine", "agent_version": "0.1.0"}


def machine(health=None, *, gpu_count=1, runtime="cuda", tiers=("container",), verified=False):
    cap = SimpleNamespace(supported_isolation_tiers=list(tiers), verification_status="VERIFIED" if verified else "DETECTED", supported_workloads=["text-generation"])
    hw = SimpleNamespace(gpu_count=gpu_count, gpu_runtime=runtime, gpu_vendor="amd" if runtime == "rocm" else "nvidia",
                         vram_mb=24576, cpu_cores=16, ram_mb=65536, disk_gb=500, raw={})
    return SimpleNamespace(is_banned=False, capability=cap, hardware=hw, region="us", stake_bonded=None, last_health=health,
                           memory_encryption_active=False, kyc_verified=False, sealed_verdict=None)


def job(**kw):
    base = dict(isolation_tier="container", gpu_count=1, required_gpu_runtime="cuda", network_policy=None, spec_v2=None,
                data_policy=None, value_at_risk=None)
    base.update(kw)
    return SimpleNamespace(**base)


def health(platform, caps, **extra):
    h = {"platform": platform, "capabilities": caps}
    h.update(extra)
    return h


FULL_LINUX = ["container_tier", "nvidia_cuda", "gvisor", "microvm", "sealed", "sealed_pro", "confidential", "egress_proxy",
              "thermal_cap", "multi_gpu", "cpu_capable", "vfio_gpu", "multi_node"]


# ---------------------------------------------------------------- W1.1 heartbeat sanitising
def test_heartbeat_health_drops_unknown_flags_and_caps_size():
    h = HeartbeatHealth.model_validate({
        "capabilities": ["container_tier", "nvidia_cuda", "root_access", "container_tier", 5, None, {"x": 1}] + ["junk"] * 500,
        "platform": {"os": "WINDOWS", "os_version": "10.0.22631", "runtime_backend": "wsl_engine", "agent_version": "1.0", "secret": "x",
                     "supervisor_version": "x" * 500},
    })
    assert h.capabilities == ["container_tier", "nvidia_cuda"]
    assert h.platform == {"os": "windows", "os_version": "10.0.22631", "runtime_backend": "wsl_engine", "agent_version": "1.0"}
    assert len(cf.sanitize_capabilities(list(cf.KNOWN_FLAGS) * 10)) == len(cf.KNOWN_FLAGS)


def test_heartbeat_health_platform_and_capabilities_are_optional_and_never_invented():
    h = HeartbeatHealth.model_validate({})
    assert h.platform is None and h.capabilities is None
    dumped = h.model_dump()
    assert dumped["platform"] is None and dumped["capabilities"] is None  # legacy agent stays legacy when stored


@pytest.mark.parametrize("bad", ["linux", 5, ["linux"], True])
def test_non_dict_platform_becomes_an_empty_flag_host_not_a_legacy_one(bad):
    assert cf.sanitize_platform(bad) == {}


def test_platform_rejects_unknown_os_and_non_ascii_values():
    assert cf.sanitize_platform({"os": "plan9", "os_version": "1"}) == {"os_version": "1"}
    assert cf.sanitize_platform({"os": "linux", "runtime_backend": "docker\nINJECT", "agent_version": "é"}) == {"os": "linux"}
    assert cf.sanitize_platform({"os": 7}) == {}


def test_capabilities_non_list_is_empty_not_absent():
    assert cf.sanitize_capabilities("container_tier") == []
    assert cf.sanitize_capabilities(None) is None


def test_stored_health_round_trips_through_the_schema():
    stored = HeartbeatHealth.model_validate(health(LINUX_PLATFORM, ["container_tier", "bogus"])).model_dump()
    host = cf.host_capabilities(machine(stored))
    assert not host.legacy and host.os == "linux" and host.flags == {"container_tier"}


# ---------------------------------------------------------------- W1.2 refusal
def test_flag_host_lacking_a_required_flag_is_refused():
    m = machine(health(LINUX_PLATFORM, ["container_tier", "nvidia_cuda"]))
    assert cf.capability_refusal(job(), m) is None
    assert cf.capability_refusal(job(isolation_tier="gvisor"), m) == "capability_missing_gvisor"
    assert cf.capability_refusal(job(isolation_tier="microvm"), m) == "capability_missing_microvm"
    assert cf.capability_refusal(job(isolation_tier="confidential"), m) == "capability_missing_confidential"
    assert cf.capability_refusal(job(isolation_tier="sealed_pro"), m) == "capability_missing_sealed_pro"
    assert cf.capability_refusal(job(isolation_tier="sealed"), m) == "capability_missing_sealed_or_sealed_pro"
    assert cf.capability_refusal(job(network_policy={"egress": ["a.example.com:443"]}), m) == "capability_missing_egress_proxy"
    assert cf.capability_refusal(job(spec_v2={"effective_resources": {"gpu_count": 0}}), m) == "capability_missing_cpu_capable"
    assert cf.capability_refusal(job(spec_v2={"effective_resources": {"node_count": 2}}), m) == "capability_missing_multi_node"
    assert cf.capability_refusal(job(spec_v2={"byoc": {"isolation": "gvisor"}}), m) == "capability_missing_gvisor"


def test_sealed_job_is_satisfied_by_either_sealed_flag_and_flags_grant_nothing_by_default():
    assert cf.capability_refusal(job(isolation_tier="sealed"), machine(health(LINUX_PLATFORM, ["container_tier", "sealed_pro"]))) is None
    assert cf.capability_refusal(job(isolation_tier="sealed"), machine(health(LINUX_PLATFORM, ["container_tier", "sealed"]))) is None
    assert cf.capability_refusal(job(isolation_tier="sealed_pro"), machine(health(LINUX_PLATFORM, ["container_tier", "sealed"]))) is not None


def test_flag_host_without_capabilities_gets_nothing():
    for h in (health(LINUX_PLATFORM, None), health(LINUX_PLATFORM, []), {"platform": LINUX_PLATFORM},
              health(LINUX_PLATFORM, "container_tier"), health(LINUX_PLATFORM, ["not_a_flag"])):
        assert cf.capability_refusal(job(), machine(h)) == "capability_missing_container_tier", h
    assert is_gpu_compatible(job(), machine({"platform": LINUX_PLATFORM}))[0] is False


def test_gpu_runtime_flag_and_multi_gpu_are_required_from_a_flag_host():
    cuda_only = machine(health(LINUX_PLATFORM, ["container_tier"]), gpu_count=2)
    assert is_gpu_compatible(job(gpu_count=1), cuda_only) == (False, "capability_missing_nvidia_cuda")
    ok = machine(health(LINUX_PLATFORM, ["container_tier", "nvidia_cuda"]), gpu_count=2)
    assert is_gpu_compatible(job(gpu_count=1), ok) == (True, None)
    assert is_gpu_compatible(job(gpu_count=2), ok) == (False, "capability_missing_multi_gpu")
    multi = machine(health(LINUX_PLATFORM, ["container_tier", "nvidia_cuda", "multi_gpu"]), gpu_count=2)
    assert is_gpu_compatible(job(gpu_count=2), multi) == (True, None)
    amd = machine(health(LINUX_PLATFORM, ["container_tier", "nvidia_cuda"]), runtime="rocm")
    assert is_gpu_compatible(job(required_gpu_runtime="rocm"), amd) == (False, "capability_missing_amd_rocm")
    amd_ok = machine(health(LINUX_PLATFORM, ["container_tier", "amd_rocm"]), runtime="rocm")
    assert is_gpu_compatible(job(required_gpu_runtime="rocm"), amd_ok) == (True, None)


def test_scheduler_tier_gate_and_v2_fit_gate_both_apply_the_flags():
    m = machine(health(LINUX_PLATFORM, ["container_tier", "nvidia_cuda"]))
    assert _machine_supports_tier_and_policy(m, job(network_policy={"egress": ["a.example.com:443"]})) == (False, "capability_missing_egress_proxy")
    assert _machine_supports_tier_and_policy(m, job()) == (True, None)
    v2 = job(spec_v2={"effective_resources": {"gpu_count": 1}, "byoc": {"isolation": "microvm"}})
    assert machine_fit_reason(v2, m) == "capability_missing_microvm"
    assert machine_fit_reason(job(spec_v2={"effective_resources": {"gpu_count": 1}}), m) is None


def test_no_flag_is_ever_granted_by_default():
    host = cf.host_capabilities(machine(health(LINUX_PLATFORM, ["container_tier"])))
    assert host.flags == {"container_tier"}
    assert cf.host_capabilities(machine(None)).flags == frozenset()


# ---------------------------------------------------------------- legacy agents unchanged
def test_legacy_agent_without_platform_object_is_unchanged_for_plain_container_jobs():
    for h in (None, {}, {"uptime_s": 5, "isolation_runtimes": ["gvisor"]}, {"capabilities": ["gvisor"]}):  # capabilities without platform: ignored
        m = machine(h)
        assert cf.host_capabilities(m).legacy is True
        assert cf.capability_refusal(job(), m) is None
        assert is_gpu_compatible(job(), m) == (True, None)
        assert _machine_supports_tier_and_policy(m, job()) == (True, None)
        assert machine_fit_reason(job(spec_v2={"effective_resources": {"gpu_count": 1}}), m) is None
    assert is_gpu_compatible(job(gpu_count=2), machine(None, gpu_count=2)) == (True, None)


def test_legacy_agent_keeps_the_existing_isolation_gate_for_byoc():
    byoc = job(spec_v2={"effective_resources": {"gpu_count": 1}, "byoc": {"isolation": "gvisor"}})
    assert machine_fit_reason(byoc, machine({"isolation_runtimes": []})) == "isolation_runtime"  # existing U8 gate, unchanged
    assert machine_fit_reason(byoc, machine({"isolation_runtimes": ["gvisor"]})) is None


# ---------------------------------------------------------------- W1.3 / X5
WINDOWS_WORK = {
    "private_sealed": job(isolation_tier="sealed"),
    "private_sealed_pro": job(isolation_tier="sealed_pro"),
    "private_confidential": job(isolation_tier="confidential"),
    "gvisor": job(isolation_tier="gvisor"),
    "microvm": job(isolation_tier="microvm"),
    "byoc": job(spec_v2={"effective_resources": {"gpu_count": 1}, "byoc": {"isolation": "gvisor"}}),
    "windows_vm": job(spec_v2={"effective_resources": {"gpu_count": 1}, "spec": {"mode": "vm", "os": "windows"}}),
    "multi_node": job(spec_v2={"effective_resources": {"gpu_count": 1, "node_count": 2}}),
}


@pytest.mark.parametrize("name", sorted(WINDOWS_WORK))
def test_windows_host_never_gets_private_byoc_isolation_vm_or_multinode(name):
    # Even when the Windows agent claims EVERY flag, X5 refuses the work.
    claiming_everything = machine(health(WINDOWS_PLATFORM, FULL_LINUX), tiers=("container", "gvisor", "microvm", "sealed", "sealed_pro", "confidential"), verified=True)
    reason = cf.capability_refusal(WINDOWS_WORK[name], claiming_everything)
    assert reason and reason.startswith("windows_host_"), (name, reason)
    assert _machine_supports_tier_and_policy(claiming_everything, WINDOWS_WORK[name])[0] is False
    assert machine_fit_reason(WINDOWS_WORK[name], claiming_everything) is not None


def test_windows_host_is_refused_even_with_no_flags_or_a_garbled_os_case():
    assert cf.capability_refusal(job(isolation_tier="sealed"), machine(health({"os": "Windows"}, None))) == "windows_host_private_tier"


def test_windows_host_still_gets_plain_container_gpu_jobs():
    m = machine(health(WINDOWS_PLATFORM, ["container_tier", "nvidia_cuda", "thermal_cap", "egress_proxy"]))
    assert cf.capability_refusal(job(), m) is None
    assert cf.capability_refusal(job(network_policy={"egress": ["a.example.com:443"]}), m) is None
    assert is_gpu_compatible(job(), m) == (True, None)
    assert _machine_supports_tier_and_policy(m, job()) == (True, None)


def test_windows_host_claiming_forbidden_flags_has_them_removed_from_its_view():
    host = cf.host_capabilities(machine(health(WINDOWS_PLATFORM, FULL_LINUX)))
    assert host.is_windows and not (host.flags & cf.WINDOWS_FORBIDDEN_FLAGS)
    assert "container_tier" in host.flags and "nvidia_cuda" in host.flags
    _plat, caps = cf.machine_platform_view(machine(health(WINDOWS_PLATFORM, FULL_LINUX)))
    assert not (set(caps) & cf.WINDOWS_FORBIDDEN_FLAGS)


def test_windows_amd_is_refused_until_it_reports_the_flag():
    m = machine(health(WINDOWS_PLATFORM, ["container_tier", "nvidia_cuda"]), runtime="rocm")
    assert is_gpu_compatible(job(required_gpu_runtime="rocm"), m) == (False, "capability_missing_amd_rocm")


# ---------------------------------------------------------------- W1.4 admin output
def test_admin_machine_view_exposes_sanitised_platform_and_flags():
    plat, caps = cf.machine_platform_view(machine(health(LINUX_PLATFORM, ["nvidia_cuda", "evil", "container_tier"])))
    assert plat == {"os": "linux", "os_version": "6.8.0", "runtime_backend": "linux_docker", "agent_version": "0.1.0"}
    assert caps == ["container_tier", "nvidia_cuda"]
    assert cf.machine_platform_view(machine(None)) == (None, None)
    assert cf.machine_platform_view(machine({"uptime_s": 3})) == (None, None)
    assert {"platform", "capabilities"} <= set(MachineOut.model_fields)
