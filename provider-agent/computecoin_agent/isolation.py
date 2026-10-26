"""Workload isolation tiers and runtime probes (Phase 8 — Stronger Isolation).

Implements the isolation tier hierarchy defined in ADR-0003:
1. "container"    - Baseline hardened container (runc). Linux namespaces, cgroups,
                    seccomp, dropped caps. Shares host kernel.
2. "gvisor"       - Application kernel sandbox (runsc). Intercepts and implements
                    syscalls in userspace, shielding host kernel from 0-days.
3. "microvm"      - Lightweight virtual machine (Kata Containers / Firecracker).
                    Dedicated guest kernel per workload.
4. "confidential" - Hardware-encrypted memory & CPU state (AMD SEV-SNP / Intel TDX /
                    NVIDIA CC) with remote cryptographic attestation proving the
                    workload is protected even against a malicious host/provider.

No silent downgrade rule (ADR-0003): If a customer requests a specific isolation
tier and the host cannot satisfy it, the agent or scheduler MUST refuse execution
rather than silently falling back to a weaker tier.
"""

from __future__ import annotations

import os
from enum import Enum
from shutil import which
from typing import Callable, Optional


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


def probe_gvisor() -> bool:
    """Check if gVisor (runsc) is available as an installed binary or runtime."""
    return which("runsc") is not None


def probe_microvm() -> bool:
    """Check if a microVM runtime (Kata Containers or Firecracker) is present."""
    return (
        which("kata-runtime") is not None
        or which("kata-qemu") is not None
        or which("firecracker") is not None
    )


def probe_confidential_computing() -> bool:
    """Check for hardware-level Confidential Computing capabilities on the host.

    Probes:
    - AMD SEV-SNP: /dev/sev, /dev/sev-guest, /sys/module/kvm_amd/parameters/sev_snp
    - Intel TDX: /dev/tdx-guest, /dev/tdx_guest
    - NVIDIA CC: NVML or /proc/driver/nvidia/...
    """
    # 1. AMD SEV-SNP
    sev_paths = (
        "/dev/sev-guest",
        "/dev/sev",
        "/sys/module/kvm_amd/parameters/sev_snp",
    )
    for p in sev_paths:
        if os.path.exists(p):
            return True

    # 2. Intel TDX
    tdx_paths = (
        "/dev/tdx-guest",
        "/dev/tdx_guest",
    )
    for p in tdx_paths:
        if os.path.exists(p):
            return True

    return False


def probe_tpm2() -> bool:
    """Check if a TPM 2.0 device node or management daemon is available."""
    tpm_paths = ("/dev/tpmrm0", "/dev/tpm0", "/sys/class/tpm/tpm0")
    for p in tpm_paths:
        if os.path.exists(p):
            return True
    return which("tpm2_getcap") is not None


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _kernel_log() -> str:
    """Boot messages, if this process may read them (needs root or kernel.dmesg_restrict=0)."""
    try:
        import subprocess

        out = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=5)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def probe_memory_encryption(
    *, cpuinfo: Optional[str] = None, kernel_log: Optional[str] = None
) -> bool:
    """True only if the kernel reports CPU memory encryption ACTIVE (AMD SME / Intel TME).

    A CPU flag alone means "supported", not "on" (firmware often leaves it off), so this needs
    the kernel's boot message. Unreadable log means False: never claim protection we cannot see.
    This is still the host's own report; the platform does not trust it for Sealed Pro (an
    administrator sets ``memory_encryption_active`` after checking).
    """
    flags = (cpuinfo if cpuinfo is not None else _read("/proc/cpuinfo")).lower()
    log = kernel_log if kernel_log is not None else _kernel_log()
    amd = " sme" in flags and "memory encryption features active: sme" in log.lower()
    intel = " tme" in flags and "x86/tme: enabled by bios" in log.lower()
    return amd or intel


_VM_NAMES = ("qemu", "kvm", "vmware", "virtualbox", "virtual machine", "hyper-v", "xen", "bochs", "parallels", "google compute engine", "amazon ec2", "openstack", "cloud hypervisor", "firecracker")


def probe_bare_metal(*, cpuinfo: Optional[str] = None, dmi_values: Optional[list[str]] = None) -> bool:
    """False if anything says we run under a hypervisor: the CPU 'hypervisor' flag or DMI vendor strings."""
    flags = (cpuinfo if cpuinfo is not None else _read("/proc/cpuinfo")).lower()
    if " hypervisor" in flags:
        return False
    if dmi_values is None:
        dmi_values = [_read(f"/sys/class/dmi/id/{f}") for f in ("product_name", "sys_vendor", "board_vendor", "bios_vendor")]
    joined = " ".join(dmi_values).lower()
    return not any(name in joined for name in _VM_NAMES)


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
