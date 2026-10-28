"""Local hardware detection.

Produces a typed :class:`CapabilityReport`. All values are SELF-REPORTED and
therefore marked ``DETECTED`` -- never ``VERIFIED``. Only a future server-side
benchmark/attestation step is allowed to promote a capability to ``VERIFIED``.

Design rules:
  * NEVER crash on a machine without a GPU. GPU detection uses NVML (pynvml /
    nvidia-ml-py) only if it imports AND initializes; otherwise we return an
    empty GPU list and a CPU-only report.
  * psutil is optional. If it is not installed we fall back to stdlib + /proc.
"""

from __future__ import annotations

import os
import platform
import shutil
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class VerificationStatus(str, Enum):
    """How much we trust a reported capability.

    DETECTED   -- self-reported by the agent (this is all the skeleton emits).
    BENCHMARKED -- measured by a local benchmark (future).
    VERIFIED   -- confirmed server-side via attestation/benchmark (future).
                  The agent itself must never set this.
    """

    DETECTED = "DETECTED"
    BENCHMARKED = "BENCHMARKED"
    VERIFIED = "VERIFIED"


class GPUInfo(BaseModel):
    index: int
    name: str
    memory_total_mb: int
    uuid: Optional[str] = None
    gpu_vendor: str = "nvidia"  # "nvidia" | "amd" (D1)
    gpu_runtime: str = "cuda"   # "cuda" | "rocm" (D1)
    gpu_runtime_version: Optional[str] = None
    # Private field for AMD render node (e.g. /dev/dri/renderD128); not sent to server
    render_node: Optional[str] = Field(default=None, exclude=True)
    status: VerificationStatus = VerificationStatus.DETECTED


class CPUInfo(BaseModel):
    logical_cores: int
    physical_cores: Optional[int] = None
    architecture: str
    model: Optional[str] = None
    status: VerificationStatus = VerificationStatus.DETECTED


class CapabilityReport(BaseModel):
    """Typed capability report for one machine.

    ``gpu_available`` is a convenience flag; ``gpus == []`` means CPU-only, which
    is a valid, fully-supported report (not an error).
    """

    hostname: str
    platform: str
    cpu: CPUInfo
    ram_total_mb: int
    disk_total_gb: int
    disk_free_gb: int
    gpus: list[GPUInfo] = Field(default_factory=list)
    gpu_available: bool = False
    isolation_tiers: list[str] = Field(default_factory=lambda: ["container"])
    # Overall trust level for the report as a whole. Always DETECTED here.
    status: VerificationStatus = VerificationStatus.DETECTED
    notes: list[str] = Field(default_factory=list)

    def summary(self) -> dict:
        """Compact summary suitable for embedding in a heartbeat payload."""
        return {
            "cpu_cores": self.cpu.logical_cores,
            "ram_mb": self.ram_total_mb,
            "disk_free_gb": self.disk_free_gb,
            "gpu_count": len(self.gpus),
            "gpu_available": self.gpu_available,
            "status": self.status.value,
        }

    def total_vram_mb(self) -> int:
        """Sum of detected GPU VRAM in MB (0 on CPU-only)."""
        return sum(g.memory_total_mb for g in self.gpus)

    def contract_summary(self) -> dict:
        """``capability_summary`` shape for the Phase 2 heartbeat contract."""
        return {
            "gpu_count": len(self.gpus),
            "vram_mb": self.total_vram_mb(),
        }

    def contract_capability_report(
        self,
        *,
        supported_workloads: Optional[list[str]] = None,
        cuda_version: Optional[str] = None,
    ) -> dict:
        """``capability_report`` shape for the Phase 2 enroll contract.

        Always honest: ``verification_status`` is the lowercase ``"detected"``
        the contract requires; the agent never emits ``"verified"`` or
        ``"benchmarked"``. ``gpus`` is ``[]`` on a CPU-only host.
        """
        return {
            "verification_status": "detected",
            "cpu_model": self.cpu.model or "unknown",
            "cpu_cores": self.cpu.logical_cores,
            "ram_mb": self.ram_total_mb,
            "disk_gb": self.disk_total_gb,
            "isolation_tiers": list(self.isolation_tiers),
            "gpus": [
                {
                    "model": g.name,
                    "vram_mb": g.memory_total_mb,
                    "cuda_version": (g.gpu_runtime_version or cuda_version) if g.gpu_runtime == "cuda" else None,
                    "gpu_vendor": g.gpu_vendor,
                    "gpu_runtime": g.gpu_runtime,
                    "gpu_runtime_version": g.gpu_runtime_version,
                }
                for g in self.gpus
            ],
            "supported_workloads": list(
                supported_workloads
                if supported_workloads is not None
                else ["text-generation", "batch-inference"]
            ),
        }


# --------------------------------------------------------------------------- #
# CPU / RAM / disk
# --------------------------------------------------------------------------- #
def _read_proc_meminfo_total_mb() -> Optional[int]:
    """Parse MemTotal from /proc/meminfo (Linux). Returns MB or None."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    # Format: "MemTotal:       16308472 kB"
                    kb = int(line.split()[1])
                    return kb // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _cpu_model() -> Optional[str]:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def detect_ram_mb() -> int:
    # Prefer psutil if present (cross-platform), else /proc, else 0.
    try:
        import psutil  # type: ignore

        return int(psutil.virtual_memory().total // (1024 * 1024))
    except Exception:
        pass
    val = _read_proc_meminfo_total_mb()
    return val if val is not None else 0


def detect_cpu() -> CPUInfo:
    logical = os.cpu_count() or 1
    physical: Optional[int] = None
    try:
        import psutil  # type: ignore

        physical = psutil.cpu_count(logical=False)
    except Exception:
        physical = None
    return CPUInfo(
        logical_cores=logical,
        physical_cores=physical,
        architecture=platform.machine() or "unknown",
        model=_cpu_model(),
        status=VerificationStatus.DETECTED,
    )


def detect_disk(path: str = "/") -> tuple[int, int]:
    """Return (total_gb, free_gb) for the filesystem holding ``path``."""
    try:
        usage = shutil.disk_usage(path)
        return usage.total // (1024**3), usage.free // (1024**3)
    except OSError:
        return 0, 0


# --------------------------------------------------------------------------- #
# GPU detection via NVML (NVIDIA) and amdsmi (AMD)
# --------------------------------------------------------------------------- #
def _detect_nvidia_gpus() -> tuple[list[GPUInfo], list[str]]:
    """Detect NVIDIA GPUs via NVML.

    Returns (gpus, notes). On any machine without a usable NVML / GPU this
    returns ([], [reason]) and NEVER raises.
    """
    notes: list[str] = []
    try:
        import pynvml  # type: ignore  # provided by nvidia-ml-py
    except Exception:
        notes.append("NVML not installed (install the 'gpu' extra); CPU-only.")
        return [], notes

    try:
        pynvml.nvmlInit()
    except Exception as exc:  # no driver, no GPU, container without passthrough
        notes.append(f"NVML present but init failed ({exc}); CPU-only.")
        return [], notes

    # The CUDA version the driver supports (e.g. 12040 -> "12.4"); not the driver's own version.
    cuda_driver_version = None
    try:
        ver = int(pynvml.nvmlSystemGetCudaDriverVersion())
        cuda_driver_version = f"{ver // 1000}.{(ver % 1000) // 10}"
    except Exception:
        pass

    gpus: list[GPUInfo] = []
    try:
        count = pynvml.nvmlDeviceGetCount()
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            try:
                uuid = pynvml.nvmlDeviceGetUUID(handle)
                if isinstance(uuid, bytes):
                    uuid = uuid.decode("utf-8", "replace")
            except Exception:
                uuid = None
            gpus.append(
                GPUInfo(
                    index=i,
                    name=name,
                    memory_total_mb=int(mem.total // (1024 * 1024)),
                    uuid=uuid,
                    gpu_vendor="nvidia",
                    gpu_runtime="cuda",
                    gpu_runtime_version=cuda_driver_version,
                    status=VerificationStatus.DETECTED,
                )
            )
    except Exception as exc:
        notes.append(f"NVML query failed ({exc}); treating as CPU-only.")
        gpus = []
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass

    if not gpus and not notes:
        notes.append("NVML initialized but reported 0 GPUs; CPU-only.")
    return gpus, notes


def _detect_amd_gpus(amdsmi_module: Any = None) -> tuple[list[GPUInfo], list[str]]:
    """Detect AMD GPUs via amdsmi (ROCm).

    Returns (gpus, notes). On any machine without usable amdsmi / AMD GPU this
    returns ([], [reason]) and NEVER raises.
    """
    notes: list[str] = []
    amdsmi = amdsmi_module
    if amdsmi is None:
        try:
            import amdsmi as _smi  # type: ignore
            amdsmi = _smi
        except Exception:
            notes.append("amdsmi not installed (install the 'amd' extra); CPU-only.")
            return [], notes

    try:
        amdsmi.amdsmi_init()
    except Exception as exc:
        notes.append(f"amdsmi present but init failed ({exc}); CPU-only.")
        return [], notes

    # Best-effort ROCm version detection
    rocm_version = None
    try:
        if os.path.isfile("/opt/rocm/.info/version"):
            with open("/opt/rocm/.info/version", "r", encoding="utf-8") as f:
                rocm_version = f.read().strip()
    except Exception:
        pass
    if not rocm_version and hasattr(amdsmi, "amdsmi_get_version"):
        try:
            v_info = amdsmi.amdsmi_get_version()
            if isinstance(v_info, dict):
                rocm_version = str(v_info.get("version") or v_info.get("rocm_version") or "")
            elif isinstance(v_info, str):
                rocm_version = v_info
        except Exception:
            pass

    gpus: list[GPUInfo] = []
    try:
        handles = amdsmi.amdsmi_get_processor_handles()
        for i, handle in enumerate(handles):
            name = "AMD Radeon / Instinct"
            try:
                asic = amdsmi.amdsmi_get_gpu_asic_info(handle)
                if isinstance(asic, dict):
                    name = asic.get("market_name") or asic.get("device_id") or name
                elif isinstance(asic, str):
                    name = asic
            except Exception:
                pass

            vram_mb = _amd_vram_total_mb(amdsmi, handle)

            uuid = None
            try:
                raw_uuid = amdsmi.amdsmi_get_gpu_device_uuid(handle)
                if raw_uuid:
                    uuid = str(raw_uuid)
            except Exception:
                uuid = None

            # Render node scoping (D6): THIS GPU's node, from AMD's own identifiers. Never by list
            # position (/dev/dri also holds other GPUs, e.g. an integrated one) and never guessed:
            # a GPU whose node cannot be resolved is not offered, because a job could not use it.
            render_node = _amd_render_node(amdsmi, handle)
            if render_node is None:
                notes.append(f"AMD GPU {i} ({name}): render node not found; not offered for jobs.")
                continue

            gpus.append(
                GPUInfo(
                    index=i,
                    name=str(name),
                    memory_total_mb=vram_mb,
                    uuid=uuid,
                    gpu_vendor="amd",
                    gpu_runtime="rocm",
                    gpu_runtime_version=rocm_version,
                    render_node=render_node,
                    status=VerificationStatus.DETECTED,
                )
            )
    except Exception as exc:
        notes.append(f"amdsmi query failed ({exc}); treating as CPU-only.")
        gpus = []
    finally:
        try:
            amdsmi.amdsmi_shut_down()
        except Exception:
            pass

    if not gpus and not notes:
        notes.append("amdsmi initialized but reported 0 GPUs; CPU-only.")
    return gpus, notes


def _amd_vram_total_mb(amdsmi: Any, handle: Any) -> int:
    """Total VRAM in MB. amdsmi_get_gpu_memory_total returns BYTES; vram_info's vram_size is MB."""
    try:
        mem_type = amdsmi.AmdSmiMemoryType.VRAM
        return int(amdsmi.amdsmi_get_gpu_memory_total(handle, mem_type)) // (1024 * 1024)
    except Exception:
        pass
    try:
        info = amdsmi.amdsmi_get_gpu_vram_info(handle)
        size = info.get("vram_size") if isinstance(info, dict) else None
        return int(size) if size not in (None, "N/A") else 0
    except Exception:
        return 0


def _amd_render_node(amdsmi: Any, handle: Any, *, sys_root: str = "/sys/bus/pci/devices", dev_root: str = "/dev/dri") -> Optional[str]:
    """The /dev/dri/renderD<N> node of this GPU: amdsmi's DRM render id when the ROCm version has
    it, else the GPU's PCI address (BDF) resolved through sysfs. None when neither works."""
    try:
        enum = amdsmi.amdsmi_get_gpu_enumeration_info(handle)
        minor = enum.get("drm_render") if isinstance(enum, dict) else None
        if isinstance(minor, int) and minor >= 128:
            node = os.path.join(dev_root, f"renderD{minor}")
            if os.path.exists(node):
                return node
    except Exception:
        pass
    try:
        bdf = str(amdsmi.amdsmi_get_gpu_device_bdf(handle)).strip().lower()
        drm_dir = os.path.join(sys_root, bdf, "drm")
        nodes = sorted(d for d in os.listdir(drm_dir) if d.startswith("renderD"))
        if len(nodes) == 1:
            node = os.path.join(dev_root, nodes[0])
            if os.path.exists(node):
                return node
    except Exception:
        pass
    return None


def detect_gpus(
    config_gpu_vendor: Optional[str] = None,
    amdsmi_module: Any = None,
) -> tuple[list[GPUInfo], list[str]]:
    """Detect available GPUs across supported vendors (NVIDIA, AMD).

    Applies locked design decision D2 (one vendor per machine):
      * If only NVIDIA is found, return NVIDIA GPUs.
      * If only AMD is found, return AMD GPUs.
      * If both are found:
          - If config_gpu_vendor is 'nvidia', return NVIDIA.
          - If config_gpu_vendor is 'amd', return AMD.
          - Without config_gpu_vendor, return [] with a clear note, and enrollment refuses.
    """
    if config_gpu_vendor is None:
        try:
            from .config import AgentConfig
            config_gpu_vendor = AgentConfig.load().gpu_vendor
        except Exception:
            config_gpu_vendor = None

    nv_gpus, nv_notes = _detect_nvidia_gpus()
    amd_gpus, amd_notes = _detect_amd_gpus(amdsmi_module=amdsmi_module)

    if nv_gpus and amd_gpus:
        chosen = (config_gpu_vendor or "").lower().strip()
        if chosen == "nvidia":
            return nv_gpus, ["Both NVIDIA and AMD GPUs detected; using NVIDIA per config."]
        elif chosen == "amd":
            return amd_gpus, ["Both NVIDIA and AMD GPUs detected; using AMD per config."]
        else:
            return [], [
                "Both NVIDIA and AMD GPUs detected without explicit config.gpu_vendor; "
                "refusing to offer GPUs (one vendor per machine)."
            ]

    if nv_gpus:
        return nv_gpus, nv_notes
    if amd_gpus:
        return amd_gpus, amd_notes

    return [], nv_notes + amd_notes


def check_dual_gpu_vendor_conflict(
    config_gpu_vendor: Optional[str] = None,
    amdsmi_module: Any = None,
) -> bool:
    """Return True if both NVIDIA and AMD GPUs are detected without an explicit vendor config."""
    if config_gpu_vendor is None:
        try:
            from .config import AgentConfig
            config_gpu_vendor = AgentConfig.load().gpu_vendor
        except Exception:
            config_gpu_vendor = None
    if config_gpu_vendor:
        return False
    nv_gpus, _ = _detect_nvidia_gpus()
    amd_gpus, _ = _detect_amd_gpus(amdsmi_module=amdsmi_module)
    return bool(nv_gpus and amd_gpus)


def build_capability_report(
    disk_path: str = "/",
    config_gpu_vendor: Optional[str] = None,
    amdsmi_module: Any = None,
) -> CapabilityReport:
    """Assemble the full capability report. Safe on CPU-only hosts."""
    from .isolation import detect_supported_isolation_tiers

    cpu = detect_cpu()
    ram = detect_ram_mb()
    disk_total, disk_free = detect_disk(disk_path)
    gpus, notes = detect_gpus(config_gpu_vendor=config_gpu_vendor, amdsmi_module=amdsmi_module)
    isolation_tiers = detect_supported_isolation_tiers()
    if not gpus:
        notes.append("No GPU detected: reporting CPU-only capabilities.")
    return CapabilityReport(
        hostname=platform.node() or "unknown",
        platform=f"{platform.system()} {platform.release()}",
        cpu=cpu,
        ram_total_mb=ram,
        disk_total_gb=disk_total,
        disk_free_gb=disk_free,
        gpus=gpus,
        gpu_available=bool(gpus),
        isolation_tiers=isolation_tiers,
        status=VerificationStatus.DETECTED,
        notes=notes,
    )

