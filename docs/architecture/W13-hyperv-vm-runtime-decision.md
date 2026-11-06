# W13: Hyper-V VM runtime on Windows hosts (gVisor / microVM-class isolation, U14 Windows VM tier): decision document

_Cross-platform plan W13.1 · Status: **default NO-GO, awaiting the owner** · Written 2026-11-06 · Research only: no production code was written for this note.
Builds on [W12](W12-windows-privacy-tiers-decision.md), [U14](U14-windows-vm-design.md), [W7](W7-amd-on-windows-decision.md) and [W8](W8-multinode-on-windows.md)._

Owner decision (plan section 9): "Windows VM tier (U14) go or no-go" is **unchanged: the owner's**. The plan lists this note as the place to decide whether a Hyper-V based runtime can give Windows
hosts the isolation tiers they lack (gVisor, microVM, and so BYOC) and the U14 Windows VM tier.

Vendor statements marked **[read 2026-11-06]** come from the Microsoft Learn pages listed in section 8. Everything else is `TODO(verify-docs)` (X11, L4): treat it as a question, not a fact.

## 1. Decision (default)

**NO-GO** for both questions, for the reasons below. Windows hosts remain container tier only (X5). BYOC stays refused on Windows (it needs gVisor or microVM). The U14 tier stays Linux-only (QEMU/KVM + VFIO, `vm_runner.py`).

## 2. What Linux gives and what is missing on Windows

| Capability | Linux host today | Windows host today | Replacement idea |
|---|---|---|---|
| gVisor (`runsc`) | user-space kernel under the container runtime | none | G below |
| microVM (Kata, KVM) | per-job microVM | no KVM on the Windows side; WSL2 may expose nested virtualization | H1, H2 |
| BYOC | needs gVisor or microVM | refused | follows the two rows above |
| U14 Windows VM with a GPU | QEMU/KVM with VFIO | none | I1, I2 |

## 3. Options for gVisor / microVM-class isolation

| Option | What it is | Verdict |
|---|---|---|
| **G. gVisor inside the WSL2 distro** | `runsc` is a Linux program; the managed distro is Linux. | **Cheapest, but gives less than it seems.** It would add a layer **between the job and the distro**, not between the job and the Windows owner: the WSL2 VM is still owned and readable by the owner (W12). It is an isolation upgrade (X1 kept: same Linux runner), not a privacy tier. Whether `runsc` works under the WSL2 kernel, which `runsc` platform it can use there, and how it interacts with the NVIDIA runtime and GPU paravirtualization are all `TODO(verify-docs)`. A candidate for a **small follow-up work package** if BYOC on Windows is ever wanted; it needs the same isolation test suite as Linux (`provider-agent/tests/escape/`). |
| **H1. Nested virtualization in WSL2 for Kata / microVM** | `.wslconfig` has a `nestedVirtualization` setting **[read 2026-11-06]**, listed as available only on Windows 11, default true. | A microVM inside the WSL2 VM: isolation of the job from the distro, again **not** from the owner. GPU access inside a nested VM is `TODO(verify-docs)` (the WSL2 GPU is paravirtualized to the distro; passing it onward to a nested guest is not something this note found any documentation for). Likely CPU-only microVMs at best. |
| **H2. A separate Hyper-V Linux VM per job, started by the supervisor** | Replace the WSL2 distro by Hyper-V VMs we manage. | Needs Hyper-V (Windows Pro/Enterprise or Server; availability on Home is `TODO(verify-docs)`), a VM image pipeline, GPU access (below), and a second runner shape: it contradicts X2 (one managed WSL2 distro) and doubles the Windows engineering. Not recommended unless the GPU question (I1/I2) is solved. |
| **H3. Windows containers (process or Hyper-V isolation)** | Windows-native containers. | **Rejected** by X12 (Kits are Linux images; Windows process isolation is not a safe boundary for strangers' code). |

## 4. Options for GPUs in a VM (needed by H2 and by the U14 tier)

| Option | Documented facts **[read 2026-11-06]** | Fit |
|---|---|---|
| **DDA** (Discrete Device Assignment) | Whole PCIe device given to one VM. Host: **Windows Server 2016 or later**. Guest: Windows Server 2016+ or Windows 10+ (Linux not listed on that page). The VM's automatic stop action must be set to TurnOff, MMIO space is configured per VM, the device is dismounted from the host (`Dismount-VMHostAssignableDevice`, with `-Force` when the vendor supplies no mitigation driver), and Microsoft warns vendors may block consumer cards. | Windows **Server** hosts only, and the owner loses that GPU for themselves. Not a gaming-PC story. Consumer GeForce support is vendor-dependent and `TODO(verify-docs)`. |
| **GPU partitioning (GPU-P)** | SR-IOV based; each VM gets a fraction; Windows Server 2025 adds live migration; supported guests include Windows 10+/Server 2019+ and Ubuntu 18.04, 20.04, 22.04; supported GPUs are server and workstation-server cards (not GeForce); partitions are auto-assigned; a physical GPU is either DDA or partitionable, not both. | Server hosts with a supported datacenter GPU. Excludes the target audience. |
| **GPU paravirtualization as used by WSL2** | WSL2 exposes the Windows GPU to the distro (`[gpu] enabled`, `/usr/lib/wsl/lib` in `wsl.conf`) **[read 2026-11-06]**. Whether the same mechanism is available to **our own** Hyper-V VMs on client Windows is `TODO(verify-docs)`. | The only path that fits gaming PCs, and the least documented one. |

**Consequence.** On consumer Windows (the target host) the documented VM-with-GPU options do not exist: DDA and GPU-P are documented for Windows Server hosts and server-class GPUs. A Hyper-V runtime that gives
a *consumer* GPU to a *Linux microVM* is not supported by anything this note could read.

## 5. The U14 Windows VM tier on a Windows host

U14 today: QEMU/KVM microVM with the GPU on VFIO, no network device, customer-supplied licensed Windows image, default off, run record and security review pending.

On a Windows host the equivalent is a Hyper-V Generation 2 Windows guest with DDA (Windows Server host) or GPU-P. Further blockers:

* Windows **guest** licensing when the host belongs to a third party and the image belongs to a customer: legal review (already an open item in U14).
* The single GPU problem: DDA dismounts the GPU from the host (the owner's display goes dark unless a second GPU exists).
* The guest-side job runner (`provider-agent/vm/guest/run-job.ps1` template) has never been run in a Windows guest.
* "No network device, input and output by disk" would be re-implemented for Hyper-V (VHDX or a Hyper-V socket channel `TODO(verify-docs)`), with a new escape test suite.
* Everything above only works on Windows Server, again excluding gaming PCs.

**Conclusion:** U14 on Windows hosts is a datacenter-Windows-Server feature at best. Keep the VM tier Linux-only.

## 6. Hardware tests that would be needed before any go

1. **G:** on a clean Windows 11 machine with the managed distro: install `runsc`, run the existing escape suite (`provider-agent/tests/escape/`) under it, plus a CUDA smoke test; record whether the GPU works at all under `runsc` in WSL2.
2. **H1:** `nestedVirtualization` on, run a Kata or Firecracker-style microVM in the distro; record CPU, memory and any GPU behaviour; confirm the owner can still read the memory (expected: yes).
3. **I (DDA/GPU-P):** a Windows Server 2025 host with a Microsoft-listed GPU and with a consumer GPU: boot a Linux guest and a Windows guest, run CUDA, reset the GPU between jobs (function-level reset behaviour varies by card: same unknown as U14), check temperature and power reporting from the host (NVML is not available for a dismounted device).
4. Isolation probes for every option: read host files, reach the host LAN, reach the supervisor control channel, exhaust host memory and disk (X6).
5. A parity run (W15 style): same Kit, same digest, equal output hashes against a Linux host.

## 7. Risks

| Risk | Impact |
|---|---|
| Treating gVisor-in-WSL2 as a privacy feature | false assurance: it is an isolation layer only |
| Two runner shapes (WSL2 distro and Hyper-V VMs) | double the code, tests and attack surface; violates X1 and X2 |
| Unsupported GPU paths (consumer GPU into a Hyper-V guest) | fragile, vendor-driver dependent, may break on any driver or Windows update |
| Hyper-V availability on Windows Home | support burden (the requirements checker would need another rule) `TODO(verify-docs)` |
| Windows Server hosts are rare among GPU owners | the work serves almost nobody |
| Windows guest licence terms | legal exposure for the platform and the host |

## 8. Recommendation

1. **NO-GO** for a Hyper-V based runtime and for U14 on Windows hosts. Record it. No code.
2. **One optional follow-up the owner may approve separately:** a small design note and test plan for **gVisor inside the managed distro (option G)**, to unlock BYOC and `gvisor` on Windows as an *isolation* tier (not privacy), only after steps 1 and 2 of section 6 pass on real hardware and X5 is amended explicitly for that single flag.
3. Reopen the Hyper-V question only if a supplier with Windows Server and supported datacenter GPUs wants to join, and then as a separate product decision with its own owner approval.

## Sources read

| Page | URL | Read |
|---|---|---|
| Partition and share GPUs with VMs on Hyper-V | <https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/gpu-partitioning> | 2026-11-06 |
| Deploy graphics devices by using Discrete Device Assignment | <https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/deploy/deploying-graphics-devices-using-dda> | 2026-11-06 |
| Advanced settings configuration in WSL (`wsl.conf`, `.wslconfig`) | <https://learn.microsoft.com/en-us/windows/wsl/wsl-config> | 2026-11-06 |
| Install WSL | <https://learn.microsoft.com/en-us/windows/wsl/install> | 2026-11-06 |

## Decision record

| Field | Value |
|---|---|
| Decision | NO-GO (default); option G is a separate, optional follow-up |
| Decided by / date | pending the owner |
| Vendor docs checked | the four pages above, 2026-11-06; everything else is `TODO(verify-docs)` |
| Revisit when | a Windows Server host with a supported GPU is offered, or the owner approves the option G design note |
