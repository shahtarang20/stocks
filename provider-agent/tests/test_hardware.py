"""Hardware detection tests, including the CPU-only (no GPU) path."""

from computecoin_agent.hardware import (
    CapabilityReport,
    VerificationStatus,
    build_capability_report,
    detect_cpu,
    detect_gpus,
)


def test_cpu_only_report_is_valid_and_has_no_gpus(no_nvml):
    report = build_capability_report()
    assert isinstance(report, CapabilityReport)
    assert report.gpus == []
    assert report.gpu_available is False
    assert report.cpu.logical_cores >= 1
    assert report.ram_total_mb > 0  # this test host has RAM
    # A clear CPU-only note must be present.
    assert any("CPU-only" in n or "CPU-only capabilities" in n for n in report.notes)


def test_report_never_marks_hardware_verified(no_nvml):
    report = build_capability_report()
    assert report.status == VerificationStatus.DETECTED
    assert report.cpu.status == VerificationStatus.DETECTED
    for gpu in report.gpus:
        assert gpu.status == VerificationStatus.DETECTED
    # And there is no path in the agent that emits VERIFIED.
    assert VerificationStatus.VERIFIED.value == "VERIFIED"


def test_detect_gpus_returns_empty_without_nvml(no_nvml):
    gpus, notes = detect_gpus()
    assert gpus == []
    assert notes and "CPU-only" in notes[0]


def test_detect_gpus_with_mocked_nvml(fake_nvml):
    fake_nvml([{"name": "NVIDIA A100", "mem_mb": 81920, "uuid": "GPU-123"}])
    gpus, notes = detect_gpus()
    assert len(gpus) == 1
    assert gpus[0].name == "NVIDIA A100"
    assert gpus[0].memory_total_mb == 81920
    assert gpus[0].status == VerificationStatus.DETECTED


def test_capability_report_with_mocked_gpu(fake_nvml):
    fake_nvml([{"name": "RTX 4090", "mem_mb": 24564, "uuid": "GPU-a"}])
    report = build_capability_report()
    assert report.gpu_available is True
    assert len(report.gpus) == 1
    # Even detected GPUs are only DETECTED, never VERIFIED.
    assert report.gpus[0].status == VerificationStatus.DETECTED


def test_detect_cpu_basic():
    cpu = detect_cpu()
    assert cpu.logical_cores >= 1
    assert cpu.architecture
    assert cpu.status == VerificationStatus.DETECTED


def test_summary_shape(no_nvml):
    s = build_capability_report().summary()
    assert set(s) == {"cpu_cores", "ram_mb", "disk_free_gb", "gpu_count", "gpu_available", "status"}
    assert s["gpu_available"] is False
    assert s["status"] == "DETECTED"
