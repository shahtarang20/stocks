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
            "gpus": [
                {
                    "model": g.name,
                    "vram_mb": g.memory_total_mb,
                    "cuda_version": cuda_version,
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
# GPU detection via NVML (optional)
# --------------------------------------------------------------------------- #
def detect_gpus() -> tuple[list[GPUInfo], list[str]]:
    """Detect NVIDIA GPUs via NVML.

    Returns (gpus, notes). On any machine without a usable NVML / GPU this
    returns ([], [reason]) and NEVER raises. This is the CPU-only path.
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


def build_capability_report(disk_path: str = "/") -> CapabilityReport:
    """Assemble the full capability report. Safe on CPU-only hosts."""
    cpu = detect_cpu()
    ram = detect_ram_mb()
    disk_total, disk_free = detect_disk(disk_path)
    gpus, notes = detect_gpus()
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
        status=VerificationStatus.DETECTED,
        notes=notes,
    )
