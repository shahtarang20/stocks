"""Tests for AMD GPU execution (executor.py, isolation.py, D5, D6)."""

from __future__ import annotations

import os
import pytest

from computecoin_agent.executor import (
    DockerExecutor,
    DockerRunResult,
    HardwareUnavailableError,
    IsolationUnavailableError,
    JobSpec,
    build_gpu_argv,
)
from computecoin_agent.isolation import (
    TIER_CONFIDENTIAL,
    TIER_CONTAINER,
    TIER_GVISOR,
    TIER_MICROVM,
)

DIGEST = "example.com/repo@sha256:456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123"


def _hw_spec(
    gpu_count: int = 1,
    tier: str = TIER_CONTAINER,
    workload_type: str = "text-generation",
) -> JobSpec:
    return JobSpec(
        job_id="job-amd-test",
        workload_type=workload_type,
        model="llama3-8b",
        input={"prompt": "hello AMD"},
        execution_mode="hardware",
        image_digest=DIGEST,
        isolation_tier=tier,
        limits={
            "memory_mb": 4096,
            "cpus": "2.0",
            "gpu_count": gpu_count,
            "max_runtime_seconds": 120,
            "pids_limit": 128,
        },
    )


def test_build_gpu_argv_nvidia_snapshot():
    """NVIDIA argv must be byte-identical to before: ['--gpus', count]."""
    assert build_gpu_argv("nvidia", 1) == ["--gpus", "1"]
    assert build_gpu_argv("nvidia", 2) == ["--gpus", "2"]
    assert build_gpu_argv("NVIDIA", 4) == ["--gpus", "4"]


def test_build_gpu_argv_amd_1_gpu():
    argv = build_gpu_argv(
        "amd",
        1,
        render_nodes=["/dev/dri/renderD128"],
        render_gid=109,
        video_gid=44,
    )
    assert argv == [
        "--device",
        "/dev/kfd",
        "--device",
        "/dev/dri/renderD128",
        "--group-add",
        "109",
        "--group-add",
        "44",
    ]


def test_build_gpu_argv_amd_2_gpus():
    argv = build_gpu_argv(
        "amd",
        2,
        render_nodes=["/dev/dri/renderD128", "/dev/dri/renderD129", "/dev/dri/renderD130"],
        render_gid=109,
        video_gid=44,
    )
    assert argv == [
        "--device",
        "/dev/kfd",
        "--device",
        "/dev/dri/renderD128",
        "--device",
        "/dev/dri/renderD129",
        "--group-add",
        "109",
        "--group-add",
        "44",
    ]


def test_amd_docker_executor_argv_exact_and_hardened():
    ex = DockerExecutor(
        docker_available=lambda: True,
        kfd_available=lambda: True,
        gpu_vendor="amd",
        render_nodes=["/dev/dri/renderD128"],
        render_gid=109,
        video_gid=44,
    )
    spec = _hw_spec(gpu_count=1)
    argv = ex.build_argv(spec, "/var/lib/ccoin/out/job-amd")

    # 1. Exact AMD GPU device flags
    expected_gpu_flags = [
        "--device",
        "/dev/kfd",
        "--device",
        "/dev/dri/renderD128",
        "--group-add",
        "109",
        "--group-add",
        "44",
    ]
    for flag in expected_gpu_flags:
        assert flag in argv

    # 2. Hardening flags (ADR-0003) MUST be present
    assert "--network" in argv and "none" in argv
    assert "--read-only" in argv
    assert "--cap-drop" in argv and "ALL" in argv
    assert "--security-opt" in argv and "no-new-privileges" in argv
    assert "--user" in argv and "65534:65534" in argv
    assert "--pids-limit" in argv and "128" in argv
    assert "--memory" in argv and "4096m" in argv
    assert "--cpus" in argv and "2.0" in argv
    assert "--tmpfs" in argv
    assert "--mount" in argv

    # 3. Forbidden flags MUST NOT be present (D6)
    assert "--privileged" not in argv
    assert "--ipc=host" not in argv
    assert "seccomp=unconfined" not in argv
    assert "--cap-add" not in argv
    assert "--gpus" not in argv  # NVIDIA-only flag

    # Whole /dev/dri must NEVER be mounted
    joined = " ".join(argv)
    assert "/dev/dri " not in joined
    assert "/dev/dri:" not in joined
    assert "card" not in joined


def test_amd_gpu_with_non_container_tier_raises_isolation_error():
    """D5: AMD GPU jobs support container isolation only in v1."""
    ex = DockerExecutor(
        docker_available=lambda: True,
        kfd_available=lambda: True,
        gvisor_available=lambda: True,
        microvm_available=lambda: True,
        confidential_available=lambda: True,
        gpu_vendor="amd",
    )

    for forbidden_tier in (TIER_GVISOR, TIER_MICROVM, TIER_CONFIDENTIAL, "sealed"):
        spec = _hw_spec(gpu_count=1, tier=forbidden_tier)
        with pytest.raises(IsolationUnavailableError) as exc_info:
            ex.build_argv(spec, "/tmp/out")
        assert "container isolation only" in str(exc_info.value)


def test_amd_cpu_job_allows_other_tiers():
    """CPU jobs on AMD host may use other isolation tiers if available."""
    ex = DockerExecutor(
        docker_available=lambda: True,
        gvisor_available=lambda: True,
        gpu_vendor="amd",
    )
    spec = _hw_spec(gpu_count=0, tier=TIER_GVISOR)
    argv = ex.build_argv(spec, "/tmp/out")
    assert "--runtime" in argv
    assert "runsc" in argv


def test_amd_gpu_missing_kfd_raises_hardware_unavailable():
    """AMD GPU job without /dev/kfd raises HardwareUnavailableError."""
    ex = DockerExecutor(
        docker_available=lambda: True,
        kfd_available=lambda: False,
        gpu_vendor="amd",
    )
    spec = _hw_spec(gpu_count=1)
    with pytest.raises(HardwareUnavailableError) as exc_info:
        ex.build_argv(spec, "/tmp/out")
    assert "/dev/kfd" in str(exc_info.value)


def test_nvidia_executor_snapshot_unchanged():
    """Verify NVIDIA DockerExecutor argv remains identical."""
    ex = DockerExecutor(
        docker_available=lambda: True,
        nvidia_available=lambda: True,
        gpu_vendor="nvidia",
    )
    spec = _hw_spec(gpu_count=2)
    argv = ex.build_argv(spec, "/tmp/out")
    assert "--gpus" in argv
    idx = argv.index("--gpus")
    assert argv[idx + 1] == "2"
    assert "--device" not in argv


def test_amd_argv_never_pads_or_guesses_render_nodes():
    """Before: missing nodes were invented (renderD128 + n) and, without detection, the first
    entries of /dev/dri were used, which can be another GPU (e.g. the integrated one)."""
    with pytest.raises(HardwareUnavailableError):
        build_gpu_argv("amd", 2, render_nodes=["/dev/dri/renderD129"], render_gid=1, video_gid=2)
    with pytest.raises(HardwareUnavailableError):
        build_gpu_argv("amd", 1, render_nodes=None, render_gid=1, video_gid=2)


def test_amd_group_ids_come_from_the_device_files(tmp_path, monkeypatch):
    """Before: hard-coded 109/44 when the group names were missing, which can be another group."""
    kfd, node = tmp_path / "kfd", tmp_path / "renderD129"
    kfd.touch()
    node.touch()
    real_stat = os.stat
    monkeypatch.setattr("computecoin_agent.executor.os.stat",
                        lambda p, *a, **k: real_stat(str(kfd) if p == "/dev/kfd" else p, *a, **k))
    argv = build_gpu_argv("amd", 1, render_nodes=[str(node)])
    assert argv[:4] == ["--device", "/dev/kfd", "--device", str(node)]
    assert argv[4:] == ["--group-add", str(real_stat(str(node)).st_gid)]  # same owner -> listed once


def test_production_executor_uses_each_gpus_detected_node(monkeypatch, tmp_path):
    """DockerExecutor() as select_executor builds it: vendor and render nodes come from detection."""
    from computecoin_agent import executor as ex_mod
    from computecoin_agent import hardware
    from computecoin_agent.hardware import GPUInfo

    detected = [GPUInfo(index=0, name="MI300X", memory_total_mb=1, gpu_vendor="amd", gpu_runtime="rocm",
                        render_node="/dev/dri/renderD130")]
    monkeypatch.setattr(hardware, "detect_gpus", lambda **kw: (detected, []))
    monkeypatch.setattr(ex_mod, "_device_gids", lambda paths: [123])  # no /dev/kfd on the test host
    monkeypatch.setattr("computecoin_agent.config.AgentConfig.load", classmethod(lambda cls, path=None: cls()))
    ex = DockerExecutor(docker_available=lambda: True, kfd_available=lambda: True)
    argv = ex.build_argv(_hw_spec(gpu_count=1), str(tmp_path))
    devices = [argv[i + 1] for i, a in enumerate(argv) if a == "--device"]
    assert devices == ["/dev/kfd", "/dev/dri/renderD130"]
    assert "--privileged" not in argv and not any(a.startswith("/dev/dri") and a.endswith("dri") for a in argv)
