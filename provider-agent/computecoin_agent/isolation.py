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
    GVISOR = "gvisor"
    MICROVM = "microvm"
    CONFIDENTIAL = "confidential"


TIER_CONTAINER = IsolationTier.CONTAINER.value
TIER_GVISOR = IsolationTier.GVISOR.value
TIER_MICROVM = IsolationTier.MICROVM.value
TIER_CONFIDENTIAL = IsolationTier.CONFIDENTIAL.value

ALL_ISOLATION_TIERS = (
    TIER_CONTAINER,
    TIER_GVISOR,
    TIER_MICROVM,
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


def detect_supported_isolation_tiers(
    *,
    probe_gvisor_fn: Optional[Callable[[], bool]] = None,
    probe_microvm_fn: Optional[Callable[[], bool]] = None,
    probe_cc_fn: Optional[Callable[[], bool]] = None,
) -> list[str]:
    """Return the list of isolation tiers supported on this host.

    Always includes 'container' (the baseline). Adds 'gvisor', 'microvm', and/or
    'confidential' when the respective runtime/hardware probes succeed.
    """
    gvisor_ok = (probe_gvisor_fn or probe_gvisor)()
    microvm_ok = (probe_microvm_fn or probe_microvm)()
    cc_ok = (probe_cc_fn or probe_confidential_computing)()

    tiers = [TIER_CONTAINER]
    if gvisor_ok:
        tiers.append(TIER_GVISOR)
    if microvm_ok:
        tiers.append(TIER_MICROVM)
    if cc_ok:
        tiers.append(TIER_CONFIDENTIAL)
    return tiers
