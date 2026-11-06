# W7: AMD GPUs on Windows hosts: decision note

_Cross-platform plan W7.3 · Status: **default NO-GO, awaiting the owner** · Written 2026-11-06 from the repository only. Every vendor
statement below is marked `TODO(verify-docs)`: nothing here was checked against AMD or Microsoft documentation (X11, L4). The
owner decision is "AMD on Windows go or no-go" (plan section 9); the plan's recommendation is no-go until the vendor-support check is done._

## Decision (default)

**No-go.** A Windows host never advertises `amd_rocm`, never runs an AMD GPU job, and the Windows runtime refuses to start one.
The code that enforces it:

* `computecoin_agent/windows/runtime/gpu.py`: `FORBIDDEN_ON_WINDOWS` contains `amd_rocm`, and `runtime_capabilities()` can only
  return flags from a closed list; a test asserts `amd_rocm` is never in the result.
* The runtime provisions the **NVIDIA** container toolkit only. There is no ROCm package, no `/dev/kfd` passthrough and no
  AMD device flag in any step.
* **Follow-up for the owner of `computecoin_agent/capabilities.py`** (another work package, not edited here): its
  `WINDOWS_FORBIDDEN_FLAGS` does not list `amd_rocm`; on Windows a detected AMD GPU with a `kfd` probe must still not set the flag.
  The server rule in `apps/api/app/services/capability_flags.py` should drop it as well.

## Why it is hard

The Linux AMD path (ADR-0003 addendum D5/D6) needs `/dev/kfd` and `/dev/dri/renderD*` inside the container and the kernel
fusion driver. Windows has no `/dev/kfd`. Whether a WSL2 guest gets those nodes depends on AMD's driver and on Microsoft's GPU
paravirtualisation, and the supported card list is narrow.

## Options

| Option | What it is | Fits X1/X2 (one Linux runner, WSL2 distro)? | Risk |
|---|---|---|---|
| A. ROCm inside the WSL2 distro | AMD's "ROCm on WSL" with the Windows Radeon driver and a ROCm userspace in the distro | yes, if it exposes a compute device to containers | Only some Radeon cards; `TODO(verify-docs)` which ones, the driver version, whether Docker can pass the device, performance, multi-GPU |
| B. Native HIP SDK on Windows | AMD HIP for Windows, the job runs as a Windows process | **no**: a second job engine and untrusted code outside a Linux container (X1, X12) | rejected |
| C. DirectML or another vendor-neutral API | Run models through DirectML | no: different Kits and images, no ROCm parity | rejected for the container tier |
| D. Linux VM with GPU partitioning | Hyper-V VM with the AMD GPU assigned | needs Windows Server and a VM runtime (W13) | out of scope until W13 |
| E. Keep AMD hosts on Linux | Linux hosts already support ROCm (D5, D6) | n/a | the default |

## Checks before any "go" (all `TODO(verify-docs)`)

1. AMD's current ROCm on WSL documentation: supported GPUs, Windows driver version, supported WSL kernel, supported ROCm and
   Ubuntu versions. Record URL and date here.
2. Whether `docker run` can be given the WSL2 compute device, and the exact flags (the Linux `--device /dev/kfd` rule of D6 may not apply).
3. Whether `amdsmi` or any telemetry works in WSL2; the Windows side would need AMD's own tooling for thermal data (not NVML).
4. Whether the smoke test (a tiny ROCm container) and `--device` selection give correct per-card isolation.
5. Isolation: the D5 rule (container tier only, no gVisor, microVM or sealed tiers for AMD) applies unchanged.
6. A real RDNA card on a clean Windows install, recorded run, before the flag is ever turned on.

## What a "go" would add (not done)

An `amd_rocm` branch in provisioning with a ROCm package pin in the manifest, a ROCm smoke test that must pass before the flag,
and removal of `amd_rocm` from `FORBIDDEN_ON_WINDOWS`. Each piece needs its own test and the vendor-doc record above.

## Decision record

| Field | Value |
|---|---|
| Decision | NO-GO (default) |
| Decided by / date | pending the owner |
| Vendor docs checked (URL, date) | none yet |
| Revisit when | AMD documents a supported Radeon list and a container path for WSL2, and a test card is available |
