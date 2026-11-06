"""Requirements checker for sharing a Windows PC's GPU (cross-platform plan 4.1 step 1; W6.2).

Used by the installer, by ``provision.ps1`` and (as JSON) by the website. It returns a structured list of results with
plain-language messages. It changes NOTHING on the machine: it only reads.

Rules (L1): a requirement that cannot be determined is ``unknown`` and BLOCKS (``report.ok`` is False) until it is resolved;
a missing probe never counts as a pass. ``warn`` does not block.

Every threshold below is a PLACEHOLDER for the owner decision "minimum Windows version and NVIDIA driver version"
(plan section 9) and every probe is an assumption about Windows, ``wsl.exe`` and ``nvidia-smi``:
TODO(verify-docs) (X11) against Microsoft's WSL system requirements
(https://learn.microsoft.com/windows/wsl/install), the WSL GPU compute guide
(https://learn.microsoft.com/windows/ai/directml/gpu-cuda-in-wsl) and NVIDIA's CUDA on WSL user guide
(https://docs.nvidia.com/cuda/wsl-user-guide/index.html). None of it was checked or run.

JSON shape (``report.to_dict()``): {"schema": 1, "ok": bool, "results": [{"id", "title", "status", "message", "fix", "detail"}]}
with status one of "pass", "fail", "warn", "unknown".
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from .wsl import RunResult, parse_version_tuple, version_at_least

SCHEMA = 1
PASS, FAIL, WARN, UNKNOWN = "pass", "fail", "warn", "unknown"
Exec = Callable[[Sequence[str]], RunResult]


@dataclass(frozen=True)
class Thresholds:
    min_windows_build: int = 19044  # Windows 10 21H2. TODO(verify-docs): exact minimum for WSL2 GPU compute
    min_wsl_version: str = "2.0.0"  # the signed manifest's min_wsl_version is authoritative at provisioning time
    min_nvidia_driver: str = "535.0"  # TODO(verify-docs): owner decision; CUDA-on-WSL needs a WSL-capable Windows driver
    min_free_disk_gb: float = 60.0
    min_ram_gb: float = 8.0
    recommended_ram_gb: float = 16.0


@dataclass(frozen=True)
class Requirement:
    id: str
    title: str
    status: str
    message: str
    fix: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "title": self.title, "status": self.status, "message": self.message, "fix": self.fix, "detail": self.detail}


@dataclass
class RequirementsReport:
    results: list[Requirement] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only when every requirement passed or is a non-blocking warning. ``unknown`` blocks."""
        return bool(self.results) and all(r.status in (PASS, WARN) for r in self.results)

    @property
    def blocking(self) -> list[Requirement]:
        return [r for r in self.results if r.status in (FAIL, UNKNOWN)]

    def to_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "ok": self.ok, "results": [r.to_dict() for r in self.results]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def summary(self) -> str:
        lines = []
        for r in self.results:
            mark = {"pass": "[ok]  ", "warn": "[warn]", "fail": "[FAIL]", "unknown": "[?]   "}[r.status]
            lines.append(f"{mark} {r.title}: {r.message}")
            if r.status in (FAIL, UNKNOWN, WARN) and r.fix:
                lines.append(f"       What to do: {r.fix}")
        return "\n".join(lines)


@dataclass
class Probes:
    """Raw facts. Each returns None when it cannot tell (never a guess). All are replaceable in tests."""

    cpu_arch: Callable[[], Optional[str]]
    windows_version: Callable[[], Optional[tuple[int, int, int]]]  # (major, minor, build)
    windows_edition: Callable[[], Optional[str]]
    virtualization: Callable[[], Optional[bool]]  # hypervisor present or virtualization enabled in firmware
    wsl_version: Callable[[], Optional[tuple[int, ...]]]
    nvidia_driver: Callable[[], Optional[str]]  # driver version text, e.g. "552.44"
    free_disk_gb: Callable[[], Optional[float]]  # on the drive that will hold the distro
    total_ram_gb: Callable[[], Optional[float]]


# --------------------------------------------------------------------------- #
# Default probes (Windows). Imports of Windows-only modules are lazy so this module imports everywhere.
# --------------------------------------------------------------------------- #
def default_exec(argv: Sequence[str]) -> RunResult:  # pragma: no cover - runs programs
    from .wsl import subprocess_runner

    return subprocess_runner(list(argv), stdin=None, timeout=60)


def default_probes(install_dir: str = "C:\\ProgramData", run: Exec = default_exec) -> Probes:  # pragma: no cover - needs Windows
    def arch() -> Optional[str]:
        return platform.machine() or None

    def win_ver() -> Optional[tuple[int, int, int]]:
        try:
            v = sys.getwindowsversion()  # type: ignore[attr-defined]  # only exists on Windows
            return (v.major, v.minor, v.build)
        except AttributeError:
            return None

    def edition() -> Optional[str]:
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
                return str(winreg.QueryValueEx(key, "EditionID")[0])
        except Exception:
            return None

    def virt() -> Optional[bool]:
        # TODO(verify-docs): Win32_ComputerSystem.HypervisorPresent and Win32_Processor.VirtualizationFirmwareEnabled via CIM.
        res = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                   "(Get-CimInstance Win32_ComputerSystem).HypervisorPresent; "
                   "(Get-CimInstance Win32_Processor | Select-Object -First 1).VirtualizationFirmwareEnabled"])
        if not res.ok:
            return None
        vals = [x.strip().lower() for x in res.stdout.splitlines() if x.strip()]
        if "true" in vals:
            return True
        return False if vals and all(v == "false" for v in vals) else None

    def wsl() -> Optional[tuple[int, ...]]:
        res = run(["wsl.exe", "--version"])
        return parse_version_tuple(res.stdout) if res.ok else None

    def nvidia() -> Optional[str]:
        res = run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
        first = res.stdout.strip().splitlines()[0].strip() if res.ok and res.stdout.strip() else ""
        return first or None

    def disk() -> Optional[float]:
        try:
            return shutil.disk_usage(install_dir).free / 1024**3
        except OSError:
            return None

    def ram() -> Optional[float]:
        try:
            import ctypes

            class _Mem(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            m = _Mem()
            m.dwLength = ctypes.sizeof(_Mem)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):  # type: ignore[attr-defined]
                return None
            return m.ullTotalPhys / 1024**3
        except Exception:
            return None

    return Probes(arch, win_ver, edition, virt, wsl, nvidia, disk, ram)


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #
def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:
        return None


def check_requirements(probes: Probes, thresholds: Thresholds = Thresholds()) -> RequirementsReport:
    t = thresholds
    out: list[Requirement] = []

    arch = _safe(probes.cpu_arch)
    if arch is None:
        out.append(Requirement("cpu_arch", "Processor type", UNKNOWN, "We could not tell what kind of processor this PC has.",
                               "Run the check again; if it still fails, contact support."))
    elif arch.lower() in ("amd64", "x86_64"):
        out.append(Requirement("cpu_arch", "Processor type", PASS, "64-bit Intel or AMD processor.", detail=arch))
    else:
        out.append(Requirement("cpu_arch", "Processor type", FAIL, f"This PC has a {arch} processor, which is not supported yet.",
                               "Sharing is only available on 64-bit Intel or AMD PCs with an NVIDIA graphics card.", arch))

    ver = _safe(probes.windows_version)
    edition = _safe(probes.windows_edition)
    if ver is None:
        out.append(Requirement("windows_version", "Windows version", UNKNOWN, "We could not read the Windows version.",
                               "Run the check again from a normal Windows session."))
    elif ver[0] < 10 or ver[2] < t.min_windows_build:
        out.append(Requirement("windows_version", "Windows version", FAIL,
                               f"Windows build {ver[2]} is too old for GPU sharing (build {t.min_windows_build} or newer is needed).",
                               "Install the latest Windows updates, or upgrade to Windows 10 21H2 or newer, or Windows 11.",
                               f"{ver[0]}.{ver[1]}.{ver[2]} edition={edition}"))
    else:
        out.append(Requirement("windows_version", "Windows version", PASS, "Your Windows version is supported.",
                               detail=f"{ver[0]}.{ver[1]}.{ver[2]} edition={edition}"))

    virt = _safe(probes.virtualization)
    if virt is None:
        out.append(Requirement("virtualization", "Virtualization", UNKNOWN, "We could not tell whether virtualization is turned on.",
                               "Open Task Manager, Performance tab, CPU, and look for 'Virtualization: Enabled'."))
    elif virt:
        out.append(Requirement("virtualization", "Virtualization", PASS, "Virtualization is turned on."))
    else:
        out.append(Requirement("virtualization", "Virtualization", FAIL, "Virtualization is turned off in this PC's firmware (BIOS/UEFI).",
                               "Restart the PC, open the BIOS/UEFI settings, turn on 'Intel Virtualization Technology' or 'SVM Mode', save and restart."))

    wsl = _safe(probes.wsl_version)
    if wsl is None:
        out.append(Requirement("wsl", "Windows Subsystem for Linux", FAIL,
                               "Windows Subsystem for Linux (WSL 2) is not installed or is too old to report its version.",
                               "Open PowerShell as administrator, run 'wsl --install --no-distribution' (or 'wsl --update'), then restart."))
    elif not version_at_least(wsl, t.min_wsl_version):
        out.append(Requirement("wsl", "Windows Subsystem for Linux", FAIL,
                               f"WSL {'.'.join(map(str, wsl))} is older than the required {t.min_wsl_version}.",
                               "Open PowerShell as administrator and run 'wsl --update'.", ".".join(map(str, wsl))))
    else:
        out.append(Requirement("wsl", "Windows Subsystem for Linux", PASS, "WSL 2 is installed and up to date.", detail=".".join(map(str, wsl))))

    drv = _safe(probes.nvidia_driver)
    if drv is None:
        out.append(Requirement("nvidia_driver", "NVIDIA graphics driver", FAIL,
                               "We could not find an NVIDIA graphics driver (or no NVIDIA graphics card).",
                               "Install the latest driver for your NVIDIA card from nvidia.com or GeForce Experience, then run the check again."))
    elif not version_at_least(parse_version_tuple(drv), t.min_nvidia_driver):
        out.append(Requirement("nvidia_driver", "NVIDIA graphics driver", FAIL,
                               f"Your NVIDIA driver ({drv}) is older than the required {t.min_nvidia_driver}.",
                               "Update the NVIDIA driver to the latest version and restart.", drv))
    else:
        out.append(Requirement("nvidia_driver", "NVIDIA graphics driver", PASS, f"NVIDIA driver {drv} is new enough.", detail=drv))

    disk = _safe(probes.free_disk_gb)
    if disk is None:
        out.append(Requirement("disk", "Free disk space", UNKNOWN, "We could not read the free disk space.", "Free up space and run the check again."))
    elif disk < t.min_free_disk_gb:
        out.append(Requirement("disk", "Free disk space", FAIL, f"Only {disk:.0f} GB are free; {t.min_free_disk_gb:.0f} GB are needed.",
                               "Free up disk space on the drive where ComputeCoin will be installed, or choose another drive.", f"{disk:.1f} GB"))
    else:
        out.append(Requirement("disk", "Free disk space", PASS, f"{disk:.0f} GB free.", detail=f"{disk:.1f} GB"))

    ram = _safe(probes.total_ram_gb)
    if ram is None:
        out.append(Requirement("ram", "Memory (RAM)", UNKNOWN, "We could not read the amount of memory.", "Run the check again."))
    elif ram < t.min_ram_gb:
        out.append(Requirement("ram", "Memory (RAM)", FAIL, f"This PC has {ram:.0f} GB of memory; at least {t.min_ram_gb:.0f} GB are needed.",
                               "Sharing needs a PC with more memory.", f"{ram:.1f} GB"))
    elif ram < t.recommended_ram_gb:
        out.append(Requirement("ram", "Memory (RAM)", WARN, f"{ram:.0f} GB of memory is enough, but {t.recommended_ram_gb:.0f} GB works better.",
                               "Jobs may be limited. Close other programs while sharing.", f"{ram:.1f} GB"))
    else:
        out.append(Requirement("ram", "Memory (RAM)", PASS, f"{ram:.0f} GB of memory.", detail=f"{ram:.1f} GB"))

    return RequirementsReport(out)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - thin wrapper
    import argparse

    ap = argparse.ArgumentParser(description="Check whether this Windows PC can share its GPU with ComputeCoin.")
    ap.add_argument("--install-dir", default="C:\\ProgramData")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = ap.parse_args(argv)
    report = check_requirements(default_probes(args.install_dir))
    print(report.to_json() if args.json else report.summary())
    return 0 if report.ok else 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
