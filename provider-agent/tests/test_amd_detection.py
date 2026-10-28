"""Tests for AMD GPU detection (ROCm / amdsmi) and dual-vendor handling."""

from __future__ import annotations

import types
import pytest
from typer.testing import CliRunner

from computecoin_agent.cli import app
from computecoin_agent.config import AgentConfig
from computecoin_agent.hardware import (
    CapabilityReport,
    VerificationStatus,
    _detect_amd_gpus,
    build_capability_report,
    check_dual_gpu_vendor_conflict,
    detect_gpus,
)


class FakeAMDHandle:
    def __init__(self, index: int):
        self.index = index


def make_fake_amdsmi(
    gpus_data: list[dict] | None = None,
    init_fails: bool = False,
    query_fails: bool = False,
    rocm_version: str = "6.2.0",
):
    if gpus_data is None:
        gpus_data = [
            {
                "market_name": "AMD Radeon RX 7900 XTX",
                "vram_mb": 24576,
                "uuid": "GPU-AMD-7900XTX-01",
            }
        ]

    mod = types.ModuleType("amdsmi")

    def _init():
        if init_fails:
            raise RuntimeError("amdsmi_init failed: driver not loaded")

    def _shutdown():
        pass

    def _handles():
        if query_fails:
            raise RuntimeError("amdsmi query failed")
        return [FakeAMDHandle(i) for i in range(len(gpus_data))]

    def _asic_info(handle):
        if query_fails:
            raise RuntimeError("amdsmi query failed")
        item = gpus_data[handle.index]
        return {"market_name": item["market_name"], "device_id": "0x744c"}

    def _vram_info(handle):  # documented: vram_size is in MB
        if query_fails:
            raise RuntimeError("amdsmi query failed")
        item = gpus_data[handle.index]
        return {"vram_type": "GDDR6", "vram_vendor": "SAMSUNG", "vram_size": item["vram_mb"], "vram_bit_width": 384}

    def _memory_total(handle, mem_type):  # documented: bytes
        if query_fails:
            raise RuntimeError("amdsmi query failed")
        assert mem_type == mod.AmdSmiMemoryType.VRAM
        return gpus_data[handle.index]["vram_mb"] * 1024 * 1024

    def _bdf(handle):
        return f"0000:{3 + handle.index:02x}:00.0"

    def _device_uuid(handle):
        if query_fails:
            raise RuntimeError("amdsmi query failed")
        item = gpus_data[handle.index]
        return item.get("uuid")

    def _version():
        return {"rocm_version": rocm_version}

    mod.amdsmi_init = _init
    mod.amdsmi_shut_down = _shutdown
    mod.amdsmi_get_processor_handles = _handles
    mod.amdsmi_get_gpu_asic_info = _asic_info
    mod.amdsmi_get_gpu_vram_info = _vram_info
    mod.amdsmi_get_gpu_memory_total = _memory_total
    mod.amdsmi_get_gpu_device_bdf = _bdf
    mod.AmdSmiMemoryType = types.SimpleNamespace(VRAM="VRAM", VIS_VRAM="VIS_VRAM", GTT="GTT")
    mod.amdsmi_get_gpu_device_uuid = _device_uuid
    mod.amdsmi_get_version = _version

    return mod


@pytest.fixture(autouse=True)
def amd_device_tree(tmp_path, monkeypatch):
    """/sys/bus/pci/devices/<bdf>/drm/renderD* and /dev/dri for up to 4 AMD GPUs at PCI buses 03..,
    plus an integrated GPU at renderD128 that must never be handed to an AMD job."""
    import functools

    from computecoin_agent import hardware

    sys_root, dev_root = tmp_path / "sys", tmp_path / "dri"
    dev_root.mkdir()
    (dev_root / "renderD128").touch()  # the iGPU
    (sys_root / "0000:00:02.0" / "drm" / "renderD128").mkdir(parents=True)
    for i in range(4):
        node = f"renderD{129 + i}"
        (sys_root / f"0000:{3 + i:02x}:00.0" / "drm" / node).mkdir(parents=True)
        (dev_root / node).touch()
    monkeypatch.setattr(hardware, "_amd_render_node",
                        functools.partial(hardware._amd_render_node, sys_root=str(sys_root), dev_root=str(dev_root)))
    return dev_root


def test_amd_gpu_detected_successfully(no_nvml):
    fake_amdsmi = make_fake_amdsmi()
    gpus, notes = detect_gpus(amdsmi_module=fake_amdsmi)
    assert len(gpus) == 1
    gpu = gpus[0]
    assert gpu.name == "AMD Radeon RX 7900 XTX"
    assert gpu.memory_total_mb == 24576
    assert gpu.uuid == "GPU-AMD-7900XTX-01"
    assert gpu.gpu_vendor == "amd"
    assert gpu.gpu_runtime == "rocm"
    assert gpu.gpu_runtime_version == "6.2.0"
    assert gpu.render_node.endswith("/renderD129")  # its own node, not the iGPU's renderD128
    assert gpu.status == VerificationStatus.DETECTED


def test_amd_detection_without_amdsmi(no_nvml):
    gpus, notes = _detect_amd_gpus(amdsmi_module=None)
    # amdsmi is not installed in the dev venv, should return empty notes without raising
    assert gpus == []
    assert any("amdsmi not installed" in n for n in notes)


def test_amd_detection_init_failure(no_nvml):
    fake_amdsmi = make_fake_amdsmi(init_fails=True)
    gpus, notes = _detect_amd_gpus(amdsmi_module=fake_amdsmi)
    assert gpus == []
    assert any("init failed" in n for n in notes)


def test_amd_detection_query_failure(no_nvml):
    fake_amdsmi = make_fake_amdsmi(query_fails=True)
    gpus, notes = _detect_amd_gpus(amdsmi_module=fake_amdsmi)
    assert gpus == []
    assert any("query failed" in n for n in notes)


def test_dual_vendor_unconfigured_refuses_gpus(fake_nvml):
    fake_nvml([{"name": "NVIDIA RTX 4090", "mem_mb": 24576, "uuid": "GPU-NV-1"}])
    fake_amdsmi = make_fake_amdsmi()

    gpus, notes = detect_gpus(config_gpu_vendor=None, amdsmi_module=fake_amdsmi)
    assert gpus == []
    assert any("Both NVIDIA and AMD GPUs detected without explicit config.gpu_vendor" in n for n in notes)
    assert check_dual_gpu_vendor_conflict(config_gpu_vendor=None, amdsmi_module=fake_amdsmi) is True


def test_dual_vendor_configured_nvidia(fake_nvml):
    fake_nvml([{"name": "NVIDIA RTX 4090", "mem_mb": 24576, "uuid": "GPU-NV-1"}])
    fake_amdsmi = make_fake_amdsmi()

    gpus, notes = detect_gpus(config_gpu_vendor="nvidia", amdsmi_module=fake_amdsmi)
    assert len(gpus) == 1
    assert gpus[0].gpu_vendor == "nvidia"
    assert gpus[0].gpu_runtime == "cuda"
    assert gpus[0].name == "NVIDIA RTX 4090"
    assert check_dual_gpu_vendor_conflict(config_gpu_vendor="nvidia", amdsmi_module=fake_amdsmi) is False


def test_dual_vendor_configured_amd(fake_nvml):
    fake_nvml([{"name": "NVIDIA RTX 4090", "mem_mb": 24576, "uuid": "GPU-NV-1"}])
    fake_amdsmi = make_fake_amdsmi()

    gpus, notes = detect_gpus(config_gpu_vendor="amd", amdsmi_module=fake_amdsmi)
    assert len(gpus) == 1
    assert gpus[0].gpu_vendor == "amd"
    assert gpus[0].gpu_runtime == "rocm"
    assert gpus[0].name == "AMD Radeon RX 7900 XTX"
    assert check_dual_gpu_vendor_conflict(config_gpu_vendor="amd", amdsmi_module=fake_amdsmi) is False


def test_capability_report_contract_includes_vendor_and_runtime(no_nvml):
    fake_amdsmi = make_fake_amdsmi()
    report = build_capability_report(amdsmi_module=fake_amdsmi)
    contract = report.contract_capability_report()
    assert len(contract["gpus"]) == 1
    gpu_entry = contract["gpus"][0]
    assert gpu_entry["gpu_vendor"] == "amd"
    assert gpu_entry["gpu_runtime"] == "rocm"
    assert gpu_entry["gpu_runtime_version"] == "6.2.0"
    assert gpu_entry["model"] == "AMD Radeon RX 7900 XTX"
    assert gpu_entry["vram_mb"] == 24576


def test_enroll_refuses_when_dual_vendor_unconfigured(monkeypatch, fake_nvml):
    fake_nvml([{"name": "NVIDIA RTX 4090", "mem_mb": 24576, "uuid": "GPU-NV-1"}])
    fake_amdsmi = make_fake_amdsmi()

    # Monkeypatch hardware detection inside cli's check
    import computecoin_agent.cli as cli
    monkeypatch.setattr(
        cli,
        "check_dual_gpu_vendor_conflict",
        lambda config_gpu_vendor=None, amdsmi_module=None: True,
    )

    runner = CliRunner()
    result = runner.invoke(app, ["enroll", "--token", "enr_test123"])
    assert result.exit_code == 1
    assert "Enrollment refused: both NVIDIA and AMD GPUs were detected" in result.output



def test_each_amd_gpu_gets_its_own_render_node_and_vram_in_mb(no_nvml):
    """Before: nodes were assigned by list position (the iGPU's renderD128 went to GPU 0) and
    VRAM was read as bytes from a field AMD documents in MB (24 GB showed as 0 MB)."""
    fake = make_fake_amdsmi([{"market_name": "AMD Instinct MI300X", "vram_mb": 196608, "uuid": f"u{i}"} for i in range(2)])
    gpus, _ = detect_gpus(amdsmi_module=fake)
    assert [g.render_node.rsplit("/", 1)[1] for g in gpus] == ["renderD129", "renderD130"]
    assert [g.memory_total_mb for g in gpus] == [196608, 196608]


def test_vram_falls_back_to_vram_info_in_mb(no_nvml):
    fake = make_fake_amdsmi()
    del fake.amdsmi_get_gpu_memory_total
    gpus, _ = detect_gpus(amdsmi_module=fake)
    assert gpus[0].memory_total_mb == 24576


def test_a_gpu_whose_render_node_cannot_be_resolved_is_not_offered(no_nvml):
    fake = make_fake_amdsmi()
    fake.amdsmi_get_gpu_device_bdf = lambda h: "0000:7f:00.0"  # no such device in sysfs
    gpus, notes = detect_gpus(amdsmi_module=fake)
    assert gpus == [] and any("render node not found" in n for n in notes)


def test_drm_render_id_from_amdsmi_is_preferred(no_nvml, amd_device_tree):
    fake = make_fake_amdsmi()
    fake.amdsmi_get_gpu_enumeration_info = lambda h: {"drm_render": 131, "drm_card": 3}
    gpus, _ = detect_gpus(amdsmi_module=fake)
    assert gpus[0].render_node == str(amd_device_tree / "renderD131")


def test_gpu_vendor_setting_is_validated():
    assert AgentConfig(gpu_vendor=" AMD ").gpu_vendor == "amd"
    assert AgentConfig(gpu_vendor="").gpu_vendor is None
    with pytest.raises(Exception):
        AgentConfig(gpu_vendor="intel")


def test_nvidia_reports_the_cuda_version_not_the_driver_version(fake_nvml, monkeypatch):
    nv = fake_nvml([{"name": "NVIDIA GeForce RTX 4090", "mem_mb": 24564}])
    nv.nvmlSystemGetCudaDriverVersion = lambda: 12040
    nv.nvmlSystemGetDriverVersion = lambda: "550.54.14"
    gpus, _ = detect_gpus(config_gpu_vendor="nvidia", amdsmi_module=make_fake_amdsmi(init_fails=True))
    assert gpus[0].gpu_runtime_version == "12.4"
    report = build_capability_report(config_gpu_vendor="nvidia", amdsmi_module=make_fake_amdsmi(init_fails=True))
    assert report.contract_capability_report()["gpus"][0]["cuda_version"] == "12.4"
