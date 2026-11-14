"""Workload isolation tiers and runtime probes (Phase 8 — Stronger Isolation).

Implements the isolation tier hierarchy defined in ADR-0003:
1. "container"    - Baseline hardened container (runc). Linux namespaces, cgroups,
                    seccomp, dropped caps. Shares host kernel.
2. "gvisor"       - Application kernel sandbox (runsc). Intercepts and implements
                    syscalls in userspace, shielding host kernel from 0-days.
3. "microvm"      - Lightweight virtual machine (Kata Containers).
                    Dedicated guest kernel per workload.
4. "confidential" - Hardware-encrypted memory & CPU state (AMD SEV-SNP / Intel TDX /
                    NVIDIA CC) with remote cryptographic attestation proving the
                    workload is protected even against a malicious host/provider.

No silent downgrade rule (ADR-0003): If a customer requests a specific isolation
tier and the host cannot satisfy it, the agent or scheduler MUST refuse execution
rather than silently falling back to a weaker tier.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from enum import Enum
from shutil import which
from typing import Callable, Optional

from .platform import get_platform


class IsolationTier(str, Enum):
    CONTAINER = "container"
    OPEN = "open"
    GVISOR = "gvisor"
    MICROVM = "microvm"
    SEALED = "sealed"
    SEALED_PRO = "sealed_pro"
    CONFIDENTIAL = "confidential"


TIER_CONTAINER = IsolationTier.CONTAINER.value
TIER_OPEN = IsolationTier.OPEN.value
TIER_GVISOR = IsolationTier.GVISOR.value
TIER_MICROVM = IsolationTier.MICROVM.value
TIER_SEALED = IsolationTier.SEALED.value
TIER_SEALED_PRO = IsolationTier.SEALED_PRO.value
TIER_CONFIDENTIAL = IsolationTier.CONFIDENTIAL.value

ALL_ISOLATION_TIERS = (
    TIER_CONTAINER,
    TIER_OPEN,
    TIER_GVISOR,
    TIER_MICROVM,
    TIER_SEALED,
    TIER_SEALED_PRO,
    TIER_CONFIDENTIAL,
)


# Docker only runs a container under a sandbox runtime it has been TOLD about (daemon.json). The program being on the disk is
# not enough: advertising a sandbox that Docker cannot use would get sandboxed jobs sent here that then fail.
_RUNTIME_CACHE_SECONDS = 60.0
_RUNTIME_FAILURE_CACHE_SECONDS = 5.0  # a failed question (daemon busy, timeout) is asked again soon, not held for a minute
_runtime_lock = threading.Lock()
_runtime_cache: "tuple[float, frozenset[str], float] | None" = None  # (when, names, how long to keep)


def _ask_docker_runtimes() -> Optional[str]:
    try:
        done = subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"], capture_output=True, text=True,
                              timeout=10, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def docker_runtimes(
    ask: Optional[Callable[[], Optional[str]]] = None, *, now: Callable[[], float] = time.monotonic
) -> frozenset:
    """Names of the runtimes Docker has registered ("runc", "runsc", "kata-qemu" ...). Empty when Docker cannot be asked.
    A good answer is kept for a minute (the heartbeat asks on every pulse); a failed one only for a few seconds."""
    global _runtime_cache
    if ask is None:
        with _runtime_lock:
            cached = _runtime_cache
        if cached is not None and now() - cached[0] < cached[2]:
            return cached[1]
    text = (ask or _ask_docker_runtimes)()  # not under the lock: a slow docker must not make other callers queue
    names: frozenset = frozenset()
    try:
        parsed = json.loads(text) if text else None
        if isinstance(parsed, dict):
            names = frozenset(str(k) for k in parsed)
    except ValueError:
        names = frozenset()
    if ask is None:
        keep = _RUNTIME_CACHE_SECONDS if names else _RUNTIME_FAILURE_CACHE_SECONDS
        with _runtime_lock:
            _runtime_cache = (now(), names, keep)
    return names


def _forget_docker_runtimes() -> None:
    """Drop the kept answer (tests, and after the host changes its Docker setup)."""
    global _runtime_cache
    with _runtime_lock:
        _runtime_cache = None


def probe_gvisor(*, runtimes: Optional[frozenset] = None, have_binary: Optional[bool] = None) -> bool:
    """True when gVisor (``runsc``) is installed AND registered with Docker as the ``runsc`` runtime."""
    binary = (which("runsc") is not None) if have_binary is None else have_binary
    return binary and "runsc" in (docker_runtimes() if runtimes is None else runtimes)


def probe_microvm(*, runtimes: Optional[frozenset] = None, have_binary: Optional[bool] = None) -> bool:
    """True when Kata Containers is installed AND registered with Docker as the ``kata-qemu`` runtime (the one the agent
    asks for). A bare Firecracker install is not enough: the agent does not start jobs through it."""
    binary = (which("kata-runtime") is not None or which("kata-qemu") is not None) if have_binary is None else have_binary
    return binary and "kata-qemu" in (docker_runtimes() if runtimes is None else runtimes)


def sandbox_hint(*, gvisor_ready: Optional[bool] = None, binary: Optional[bool] = None, runtimes: Optional[frozenset] = None) -> str:
    """One plain sentence for the host about the optional sandbox, saying exactly what is missing. No jargon."""
    rts = docker_runtimes() if runtimes is None else runtimes
    have = (which("runsc") is not None) if binary is None else binary
    ready = probe_gvisor(runtimes=rts, have_binary=have) if gvisor_ready is None else gvisor_ready
    if ready:
        return "Sandbox (gVisor) is ready: this computer can also take sandboxed jobs."
    if have and rts:
        return ("gVisor is installed but Docker does not know about it, so sandboxed jobs are not offered. "
                "Run `sudo runsc install` and restart Docker.")
    if not rts:
        return "Sandbox (gVisor) is not set up (Docker could not be asked, or it has no sandbox). Ordinary jobs only."
    return ("Sandbox (gVisor) is not installed: this computer takes ordinary jobs only. Installing gVisor and registering "
            "it with Docker lets it also take sandboxed jobs.")


def probe_confidential_computing() -> bool:
    """Check for hardware-level Confidential Computing capabilities on the host.

    Probes (in the platform layer): AMD SEV-SNP and Intel TDX device nodes. Unknown means False.
    """
    return _hw().has_confidential_device()


def probe_tpm2() -> bool:
    """Check if a TPM 2.0 device node or management daemon is available."""
    if _hw().has_tpm_device():
        return True
    return which("tpm2_getcap") is not None


def _hw():
    return get_platform().hardware


def probe_memory_encryption(
    *, cpuinfo: Optional[str] = None, kernel_log: Optional[str] = None
) -> bool:
    """True only if the kernel reports CPU memory encryption ACTIVE (AMD SME / Intel TME).

    A CPU flag alone means "supported", not "on" (firmware often leaves it off), so this needs
    the kernel's boot message. Unreadable log means False: never claim protection we cannot see.
    This is still the host's own report; the platform does not trust it for Sealed Pro (an
    administrator sets ``memory_encryption_active`` after checking).
    """
    flags = (cpuinfo if cpuinfo is not None else _hw().cpu_flags_text()).lower()
    log = kernel_log if kernel_log is not None else _hw().kernel_log()
    amd = " sme" in flags and "memory encryption features active: sme" in log.lower()
    intel = " tme" in flags and "x86/tme: enabled by bios" in log.lower()
    return amd or intel


_VM_NAMES = ("qemu", "kvm", "vmware", "virtualbox", "virtual machine", "hyper-v", "xen", "bochs", "parallels", "google compute engine", "amazon ec2", "openstack", "cloud hypervisor", "firecracker")


def probe_bare_metal(*, cpuinfo: Optional[str] = None, dmi_values: Optional[list[str]] = None) -> bool:
    """False if anything says we run under a hypervisor: the CPU 'hypervisor' flag or DMI vendor strings."""
    flags = (cpuinfo if cpuinfo is not None else _hw().cpu_flags_text()).lower()
    if " hypervisor" in flags:
        return False
    if dmi_values is None:
        dmi_values = _hw().dmi_values()
    joined = " ".join(dmi_values).lower()
    return not any(name in joined for name in _VM_NAMES)


def probe_iommu(*, iommu_dir: Optional[str] = None) -> bool:
    """True if the kernel has at least one active IOMMU (DMA remapping) unit."""
    return _hw().iommu_active(iommu_dir)


def probe_kernel_lockdown(*, lockdown_file: Optional[str] = None) -> bool:
    """True if kernel lockdown is in 'integrity' or 'confidentiality' mode (the bracketed one is active)."""
    return _hw().kernel_lockdown_active(lockdown_file)


def measure_security_state() -> dict[str, bool]:
    """The locks the attestation verifier requires, as this host measures them right now."""
    return {"bare_metal": probe_bare_metal(), "iommu_enabled": probe_iommu(), "kernel_lockdown": probe_kernel_lockdown()}


def detect_supported_isolation_tiers(
    *,
    probe_gvisor_fn: Optional[Callable[[], bool]] = None,
    probe_microvm_fn: Optional[Callable[[], bool]] = None,
    probe_cc_fn: Optional[Callable[[], bool]] = None,
    probe_tpm_fn: Optional[Callable[[], bool]] = None,
    probe_mem_enc_fn: Optional[Callable[[], bool]] = None,
    include_sealed: bool = False,
) -> list[str]:
    """Return the list of isolation tiers supported on this host.

    Always includes 'container' (the baseline). Adds 'gvisor', 'microvm', and/or
    'confidential' when the respective probes succeed.
    Adds 'sealed' and 'sealed_pro' when TPM and memory encryption probes succeed.
    """
    gvisor_ok = (probe_gvisor_fn or probe_gvisor)()
    microvm_ok = (probe_microvm_fn or probe_microvm)()
    cc_ok = (probe_cc_fn or probe_confidential_computing)()

    tiers = [TIER_CONTAINER]
    if gvisor_ok:
        tiers.append(TIER_GVISOR)
    if microvm_ok:
        tiers.append(TIER_MICROVM)
    if probe_tpm_fn is not None or include_sealed:
        tpm_ok = probe_tpm_fn() if probe_tpm_fn else probe_tpm2()
        if tpm_ok:
            tiers.append(TIER_SEALED)
            mem_enc_ok = probe_mem_enc_fn() if probe_mem_enc_fn else probe_memory_encryption()
            if mem_enc_ok:
                tiers.append(TIER_SEALED_PRO)
    if cc_ok:
        tiers.append(TIER_CONFIDENTIAL)
    return tiers
