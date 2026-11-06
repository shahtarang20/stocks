# Cross-platform (Linux + Windows): master architecture, master prompt and master checklist

_Status date: 2026-11-06 · Owner: engineering · Status: **design, not started** ·
Baseline: `main` at `7770d70` (all code written in the previous plans is **unrun**; nothing in this
document has been tested on Windows)._

This is the single plan for making **every role** (customer, host, admin) work on **Linux and Windows**.
It follows the format of [production-readiness-master-plan.md](production-readiness-master-plan.md) and
[remaining-code-work-master-plan.md](remaining-code-work-master-plan.md): scope, locked decisions,
architecture, work packages, master checklist, master prompt, report format.

| Read first | Why |
|---|---|
| [remaining-code-work-master-plan.md](remaining-code-work-master-plan.md) | Locked decisions L1–L11 still apply here |
| [ADR-0003 Isolation](../architecture/ADR-0003-isolation.md) | Container hardening every Windows path must keep |
| [ADR-0005 Proof-of-Privacy](../architecture/ADR-0005-proof-of-privacy.md) | Why privacy tiers cannot simply be "ported" |
| [ADR-0006 Universal workloads](../architecture/ADR-0006-universal-workloads.md) | Kits, runtime v2, sessions, services |
| [U14 Windows VM design](../architecture/U14-windows-vm-design.md) | Running Windows-only **software** (different from Windows **hosts**) |

---

## 1. The honest goal

"Offer everything on Windows that we offer on Linux" is the target. It is reachable for **most** of the
platform. A few capabilities depend on Linux kernel features, Linux-only hardware attestation or AMD's Linux-only
GPU stack. For those, this plan does **not** pretend: each one gets a **named replacement path** or an explicit
**owner decision**, and the system **never advertises a capability it cannot prove** (X4).

Two different things are called "Windows" and must not be confused:

| Meaning | Example | Where it is handled |
|---|---|---|
| A **Windows host machine** running our agent (a Windows PC or server that shares its GPU) | gaming PC with an RTX card | **This plan** |
| **Windows-only software** run for a customer (3ds Max, some renderers) | job needs a Windows app | U14 Windows VM tier (separate, optional) |

### 1.1 Status legend

| Code | Meaning |
|---|---|
| **P** | Parity: same code, same behaviour on both |
| **E** | Equivalent: different mechanism, same customer-visible result |
| **L** | Limited: a documented subset |
| **R** | Research / owner decision needed before it can be promised |
| **N** | Not possible with current OS or hardware; replacement path named |

### 1.2 Capability parity matrix

| Capability | Linux host | Windows host | Status | Note |
|---|---|---|---|---|
| **Customer** | | | | |
| Web console, marketplace, wizard | yes | yes (any browser) | **P** | Web app is OS-neutral |
| Python / TypeScript SDK, private-chat helpers | yes | yes | **P** | Needs a Windows test run (W9) |
| Resumable uploads, scoped downloads | yes | yes | **P** | Browser and SDK paths |
| Kit jobs, fan-out, pipelines, quotes | yes | yes | **P** | Same Linux images (X12) |
| Sessions (terminal, HTTP proxy), services | yes | yes | **P** | Browser side is identical |
| **Host: container tier** | | | | |
| NVIDIA GPU jobs in containers | native Docker | WSL2 managed runtime | **E** | W3, W4 |
| AMD GPU jobs (ROCm) | `/dev/kfd` | ROCm on WSL2 only for some cards | **R** | W7; verify vendor support (L4) |
| Thermal cap, hard stop, power limit | NVML / amdsmi | native NVML from the supervisor | **E** | W7 |
| Multi-GPU on one host | `--gpus device=...` | same inside WSL2 | **E** | Verify device selection (L4) |
| Egress allowlist proxy (B5) | yes | same code inside the runtime distro | **P** | Runs in the Linux runner |
| Private Kit jobs on RAM-only dir | tmpfs | n/a, see privacy rows | **N** | X5 |
| Idle / foreground-app yield, schedules | optional | **required** (PC is also the owner's) | **E** | W5 |
| **Host: stronger isolation and privacy** | | | | |
| gVisor | `runsc` | none | **N** | Replacement: dedicated VM runtime (R, W12) |
| microVM | Kata/KVM | none on consumer Windows | **N** | Replacement: Hyper-V VM (R, W13) |
| BYOC (needs gVisor or microVM) | yes | refused | **N** | Fail closed until a VM runtime exists |
| Sealed, Sealed Pro (TPM + Sealed OS image) | yes | none | **N** | Windows owner can read VM memory; W12 research |
| Confidential (SEV-SNP / TDX + NVIDIA CC) | yes | none | **N** | Needs confidential-VM hardware and a different attestation |
| Multi-node (U13) | yes | WSL2 NAT blocks it by default | **R** | W8: mirrored networking or bridged distro |
| Windows VM tier (U14) | QEMU/KVM + VFIO | no KVM | **R** | W13: Hyper-V GPU partitioning |
| **Host operations** | | | | |
| Install, enrol, start on boot | systemd / script | MSI + Windows Service | **E** | W2, W6 |
| Secret storage | file 0600 | DPAPI / Credential Manager | **E** | X7 |
| Auto-update with rollback | script | signed updater | **E** | W6 |
| Survive sleep, reboot, Windows Update | systemd | power-event handling | **E** | W5 |
| **Admin** | | | | |
| Web admin console | yes | yes | **P** | |
| Fleet view with platform and capabilities | none today | new | **P** | W1, W10 |
| Operator scripts (setup, kit signing, vectors) | bash | PowerShell equivalents | **E** | W10 |
| Control plane (API, worker, DB) | Linux containers | **not a target** | **N by design** | X3: Windows developers use WSL2 or Docker |

Every **N** row has a replacement path, a research package or an owner decision in section 9. None is silently dropped.

---

## 2. Locked decisions (apply to every package)

| ID | Decision |
|---|---|
| **X1 One runner** | Jobs run through **one** Linux code path on every OS: the existing executor, runtime v2, sessions and services code. Windows adds a thin native shell, not a second job engine. |
| **X2 Windows host shape** | A Windows host = a **native Windows supervisor** (Windows Service) plus a **managed, dedicated WSL2 distro** that contains the Linux runner, Docker Engine and the NVIDIA container toolkit. The agent never runs a job outside a Linux container or VM. |
| **X3 Control plane stays Linux** | API, worker, database and Redis ship as Linux containers. Windows contributors develop through WSL2 or Docker. No Windows-server build of the control plane. |
| **X4 Advertise only what is proven** | Every host reports a `platform` object and a list of **capability flags** in its heartbeat. The scheduler only places work that needs a flag on hosts that report it. A missing flag means refused, never defaulted. |
| **X5 No privacy tier on Windows until approved** | Sealed, Sealed Pro, Confidential, gVisor, microVM and BYOC are **not** offered on Windows hosts until a Windows attestation and isolation design is approved by the owner and tested on real hardware (W12, W13). |
| **X6 Windows files unreachable from jobs** | In the runtime distro: automount **off**, Windows interop **off**, Windows PATH not appended, no drive mounts into containers. A job must never see a Windows file. |
| **X7 OS secret stores** | Credentials live in DPAPI or Credential Manager on Windows, and keep the 0600 file on Linux. No plain-text secret in a Windows user profile. |
| **X8 Platform Abstraction Layer** | All OS-specific code sits behind interfaces in `provider-agent/computecoin_agent/platform/`. Code outside it must not call `os.getuid`, `os.mkfifo`, `os.chmod` for security, read `/proc`, `/dev` or `/sys`, or assume a path separator. A test enforces this. |
| **X9 Paths** | Host paths use `pathlib`. In-container paths are always `PurePosixPath`. Never join paths by string. |
| **X10 Signed everything** | Windows installer is Authenticode-signed. Update manifests are signed and verified. The runtime rootfs is pinned by digest. A failed signature check stops the install or update. |
| **X11 Verify external interfaces** | Every WSL2, Docker, NVIDIA, AMD, Windows API and installer tool interface is checked against current official documentation before use, with URL and date recorded next to the code (same rule as L4). Memory is not evidence. |
| **X12 One image per Kit** | Kits are Linux images. A Windows host runs the **same** image digest as a Linux host. Windows-only software uses the U14 VM tier, never a Windows container for untrusted work. |
| **X13 Host yields to its owner** | By default the agent does not take the GPU while the owner is using it (foreground app or recent input), and never during a full-screen app. This applies on Linux desktops too. |
| **X14 Informed consent** | The installer shows what will be installed, what resources are shared, and how to stop and remove everything. Uninstall removes the distro, service, firewall rules and credentials. |
| **X15 Inherited rules** | L1 fail closed, L2 one agent at a time, L3 money code read-only, L5 tests prove behaviour, L7 no secrets, L11 stay in `/home/root509/stocks` all apply unchanged. |

---

## 3. Reference architecture

```
                         CONTROL PLANE  (Linux containers, unchanged: X3)
        +-----------------------------------------------------------------------+
        |  API  ·  Worker  ·  Postgres  ·  Redis  ·  Object storage (data plane)  |
        |  Scheduler: places work only on hosts advertising the needed capability |
        +--------------^----------------------------^---------------------------+
                       | HTTPS (outbound only)      | HTTPS
                       |                            |
   +-------------------+-------------+      +-------+----------------------------+
   |  LINUX HOST                     |      |  WINDOWS HOST                      |
   |                                 |      |                                    |
   |  systemd service                |      |  Windows Service (native)          |
   |   |                             |      |   - lifecycle, updates, secrets    |
   |   v                             |      |   - NVML telemetry, thermal, power |
   |  RUNNER (Python)  <-- same code -+------+-> - idle / foreground yield        |
   |   - executor, runtime v2        |      |   - power / sleep / reboot events  |
   |   - sessions, services          |      |   - starts and watches the distro  |
   |   - private-kit, egress proxy   |      |        | control channel           |
   |   |                             |      |        v                           |
   |   v                             |      |  Dedicated WSL2 distro "computecoin"|
   |  Docker Engine + GPU runtime    |      |   RUNNER (same code as Linux)      |
   |   |                             |      |   Docker Engine + NVIDIA toolkit   |
   |   v                             |      |        |                           |
   |  Job containers (hardened)      |      |        v                           |
   +---------------------------------+      |   Job containers (hardened)        |
                                            +------------------------------------+

   CUSTOMER (any OS): browser · Python SDK · TypeScript SDK · uploads direct to storage
   ADMIN (any OS):    browser console · PowerShell or bash operator tooling · CI matrix
```

### 3.1 Why this shape

| Option | Verdict | Reason |
|---|---|---|
| Run the whole agent natively on Windows, drive Linux Docker | rejected | Needs a second path translation layer everywhere (bind mounts, devices, tmpfs, FIFOs). More code, more bugs, less secure. |
| Run only inside WSL2 with no native shell | rejected | WSL2 sees no Windows power events, no foreground-app state, no Windows service lifecycle, and native NVML is the better telemetry source on Windows. |
| **Native supervisor + managed WSL2 distro running the unchanged Linux runner** | **chosen (X1, X2)** | One job path. The Windows-specific code is small, auditable and about operations only. |
| Windows containers | rejected for untrusted work | Process isolation is not a safe boundary for strangers' code (X12). |

### 3.2 Platform Abstraction Layer (X8)

`provider-agent/computecoin_agent/platform/` exposes one interface per concern, with `linux.py`, `windows.py` and a
test fake. Nothing outside it may know which OS it is on.

| Interface | Linux implementation | Windows implementation |
|---|---|---|
| `Paths` | `~/.computecoin`, `/dev/shm` | `%ProgramData%` and `%LOCALAPPDATA%` for host files; the runner's paths stay POSIX inside the distro |
| `Secrets` | 0600 file | DPAPI or Credential Manager |
| `ServiceControl` | systemd | Windows Service control manager |
| `Signals / shutdown` | SIGTERM handlers | service stop events, console control events |
| `FifoOrPipe` | `os.mkfifo` | named pipe, or not needed because the runner is in Linux |
| `OwnerIdentity` | `os.getuid` | unused outside the runner; supervisor uses SIDs |
| `RamWorkdir` | tmpfs check via `/proc/mounts` | inside the distro only |
| `HardwareProbe` | `/proc`, `/sys`, `nvidia-ml-py`, `amdsmi` | `psutil`, WMI, native NVML |
| `GpuControl` | NVML, amdsmi | native NVML (power limit needs admin, verify) |
| `PowerState` | none | sleep, hibernate, reboot-pending, battery |
| `ActivityProbe` | optional X idle | foreground app, last input, full-screen detection |
| `RuntimeBackend` | `LinuxDocker` | `WslEngine` (default), `DockerDesktop` (optional, owner decision) |
| `Updater` | script | signed MSI or app updater with rollback |

### 3.3 Supervisor to runner control channel

The supervisor starts and stops the runner inside the distro and exchanges a small, versioned set of messages:

| Direction | Messages |
|---|---|
| supervisor to runner | `start`, `drain` (finish current job, claim no more), `stop`, `set_limits` (thermal caps, concurrency), `set_availability` |
| runner to supervisor | `ready`, `job_started`, `job_finished`, `error`, `usage` |
| supervisor to API | heartbeat with `platform` object, capability flags, native GPU telemetry, `throttled` or `yielding` availability |

Transport: a loopback socket bound to the distro's address with a per-install random token (never an open port to the
LAN), or `wsl.exe` stdio. The runner **also** keeps working with no supervisor (Linux behaviour unchanged).

### 3.4 Capability advertisement (X4)

Heartbeat gains `platform` and `capabilities` (additive, server-sanitised):

```json
{
  "platform": {"os": "windows", "os_version": "10.0.22631", "runtime_backend": "wsl_engine",
               "agent_version": "x.y.z", "supervisor_version": "x.y.z"},
  "capabilities": ["container_tier", "nvidia_cuda", "egress_proxy", "thermal_cap", "multi_gpu"]
}
```

Known flags: `container_tier`, `nvidia_cuda`, `amd_rocm`, `gvisor`, `microvm`, `sealed`, `sealed_pro`, `confidential`,
`vfio_gpu`, `multi_node`, `egress_proxy`, `thermal_cap`, `power_limit`, `multi_gpu`, `cpu_capable`. Unknown flags are
dropped. The scheduler maps each job requirement to flags and refuses hosts that lack them. Windows hosts initially
report only the container-tier flags (X5).

---

## 4. Host architecture (Windows)

### 4.1 Installation and provisioning

1. **Requirements checker** (also exposed on the website): Windows version, virtualization enabled in firmware, WSL2
   available, NVIDIA driver version, free disk, RAM. Output is a plain-language pass or fail list.
2. **Installer** (MSI or MSIX plus `winget` manifest; owner decision), Authenticode-signed (X10). Installs the
   supervisor as a service, registers firewall rules (outbound only), creates the data directory.
3. **Runtime distro provisioning:** import a **pinned rootfs** (verified by digest) as a dedicated distro `computecoin`,
   apply `wsl.conf` hardening (X6), enable systemd, install Docker Engine and the NVIDIA container toolkit from pinned
   versions, install the runner, run a **GPU smoke test** (a tiny CUDA container) before enrolment succeeds.
4. **Enrolment:** the existing enrolment flow, using Secrets (X7). The host sees a consent summary first (X14).
5. **Repair and reset:** one command to re-provision the distro without touching credentials; one to uninstall everything.

Distro hardening (values to be verified, X11): `[automount] enabled=false`, `[interop] enabled=false`,
`appendWindowsPath=false`, `[boot] systemd=true`, non-root runner user, no SSH, no published ports, memory and CPU limits
from the supervisor, a capped virtual disk size.

### 4.2 Running well on a PC the owner also uses (X13)

| Concern | Behaviour |
|---|---|
| Owner is gaming or working | `yielding` availability: no new claims while a foreground app uses the GPU or input is recent; running job drains, not killed, unless the owner chooses "stop now" |
| Full-screen app | never claim, never start a job |
| Schedules | owner sets allowed windows; defaults conservative |
| Sleep, hibernate | prevent sleep only while a job runs (execution-state request); otherwise allow |
| Windows Update reboot | detect pending reboot, drain, report `maintenance` |
| Laptop on battery | do not claim |
| GPU memory pressure | respect a configurable VRAM reserve for the owner |
| Disk and RAM | capped distro disk, memory cap through the distro config; document that WSL2 limits are per user |
| Thermal | native NVML temperature and fan data drive the existing thermal guard (B6) |

### 4.3 Updates

Signed update manifest, staged rollout rings (admin-controlled, section 6), automatic rollback if the new version fails
its post-update health check, never update during a job.

### 4.4 Security model for Windows hosts

| Boundary | Statement |
|---|---|
| Host owner vs job | The owner is **not trusted for confidentiality** (same as the Linux container tier). The WSL2 VM memory is readable by the Windows host. Hence no privacy tier (X5). |
| Job vs host | A job must not touch Windows: automount off, interop off (X6), non-root containers, `--read-only`, caps dropped, no host mounts, no privileged mode (ADR-0003 unchanged). |
| Job vs LAN | Default no network. Egress only through the pinned allowlist proxy (B5). Windows firewall allows the supervisor outbound only. |
| Supervisor vs runner | Authenticated local channel with a random token; no remote listener. |
| Credentials | DPAPI or Credential Manager; never logged (L7). |
| Supply chain | Signed installer, pinned rootfs and tool versions, signed updates (X10). |
| Mining and abuse | Existing abuse signals apply; the Windows supervisor also reports process names it can see on the host side for the runtime distro only. |

---

## 5. Customer architecture (Windows and Linux)

| Area | Requirement |
|---|---|
| Web | Already OS-neutral. Test the current browsers on Windows (Edge, Chrome, Firefox) including uploads, Web Worker hashing, the session Service Worker and the proxy page. |
| Python SDK | Pure Python plus `cryptography`. Test on Windows: file paths, line endings in sealed payloads (always UTF-8 and canonical JSON), temp files, async on the Windows event loop. |
| TypeScript SDK | `node:crypto` is cross-platform. Test on Windows Node. Browser use stays dynamic-import safe. |
| Uploads | Resumable multipart works from the browser on any OS; the SDK upload helper must use binary mode and `pathlib`. |
| Customer CLI (if shipped) | Same behaviour on Windows Terminal and PowerShell; no bash-only instructions in docs. |
| Docs | Every customer how-to shows Windows and Linux commands side by side. |

---

## 6. Admin and operator architecture

| Area | Requirement |
|---|---|
| Fleet view | Admin console shows each host's OS, runtime backend, supervisor and runner versions, capability flags, last-seen, `yielding` / `throttled` state. Filters by platform. |
| Scheduling visibility | For any stuck job, show which capability flag no host provided. |
| Release management | Agent and supervisor versions per platform, staged rollout rings (canary, early, general), pause and rollback, forced-update floor for security fixes. |
| Support bundle | One command on the host produces a redacted diagnostics zip (versions, requirement results, runtime health, last logs, no secrets). |
| Operator scripts | Every `scripts/*.sh` has a PowerShell equivalent or is rewritten in Python (kit signing, vector generation, network setup, key generation). One tested entry point per task. |
| CI | Matrix: Linux and Windows for agent unit tests, SDKs, kit CI gates; a **self-hosted Windows runner with an NVIDIA GPU** for the end-to-end host tests (hosted Windows runners cannot be assumed to provide WSL2 with GPU). |
| Observability | Metrics gain a `platform` label; alert rules for Windows-specific failures (distro unhealthy, GPU smoke test failing, update rollbacks). |
| Docs | Admin runbooks cover both platforms. |

---

## 7. Work packages

Sizes are estimates for planning only. **P0 packages must land first and must not change Linux behaviour.**

| ID | Package | Depends on | Size | Gate |
|---|---|---|---|---|
| **W0** | Platform Abstraction Layer; remove Linux-only calls from shared code (`os.getuid`, `os.mkfifo`, `/proc`, `/dev` outside PAL); add `psutil` dependency; lint test for X8 | none | Medium | none |
| **W1** | Capability advertisement: `platform` + `capabilities` in heartbeat; server sanitises, stores, schedules by flag; admin shows it | W0 | Small–medium | none |
| **W2** | Windows supervisor: Windows Service, logging, DPAPI secrets, config paths, graceful stop, enrolment | W0 | Medium | signing cert (owner) |
| **W3** | Managed WSL2 runtime distro: pinned rootfs, hardening (X6), Docker Engine, NVIDIA toolkit, GPU smoke test, repair, reset | W2 | Large | Docker Desktop vs Engine decision |
| **W4** | Supervisor to runner control channel; telemetry and job lifecycle bridge; drain and stop semantics | W2, W3 | Medium–large | none |
| **W5** | Resource governance: activity probe, yield, schedules, power events, Windows Update, VRAM reserve, disk and memory caps | W4 | Medium | default policy (owner) |
| **W6** | Installer and distribution: MSI or MSIX, winget, Authenticode, requirements checker, uninstall, signed updater with rollback and rings | W2, W3 | Medium | installer tech, cert |
| **W7** | Windows GPU features: native NVML thermal and power, multi-GPU device selection, AMD research and go or no-go | W4 | Medium | AMD decision |
| **W8** | Networking: egress proxy verified in the distro; multi-node feasibility (mirrored networking or bridged distro), documented decision | W3 | Medium | multi-node decision |
| **W9** | Customer side on Windows: SDKs, uploader, web tests, docs | none | Small | none |
| **W10** | Admin and operator: fleet views, rollout rings, support bundle, PowerShell tooling, CI matrix, metrics label | W1 | Medium | none |
| **W11** | Security: threat model, hardening review, supply-chain checks, abuse signals on Windows | W3, W4 | Medium | independent review |
| **W12** | **Research:** privacy tiers on Windows (VM-based runtime, vTPM, Windows Server options, attestation design); decision document only | W11 | Research | owner go or no-go |
| **W13** | **Research:** Hyper-V VM runtime for gVisor/microVM-class isolation and the U14 Windows VM tier on Windows hosts | W12 | Research | owner go or no-go |
| **W14** | Docs and website: host guide, requirements checker page, troubleshooting, "Share my computer" Windows section | W6 | Small | none |
| **W15** | Test infrastructure: Windows CI jobs, self-hosted Windows+NVIDIA runner, end-to-end host test | W3, W4 | Medium | runner hardware (owner) |

**Recommended order:** W0 → W1 → W9 → W2 → W3 → W4 → W15 → W5 → W7 → W6 → W10 → W8 → W14 → W11, then W12 and W13 as research.

---

## 8. Test strategy

| Layer | What | Where it runs |
|---|---|---|
| PAL unit tests | Every interface with fakes; X8 lint | Linux and Windows hosted CI |
| Agent unit tests | Existing agent suite on Windows Python | Windows hosted CI |
| SDK tests | Python and TypeScript on Windows | Windows hosted CI |
| Supervisor tests | Service lifecycle, secrets, power events, with fakes | Windows hosted CI |
| Provisioning test | Create the distro, run the GPU smoke test, run one real Kit job end to end | **Self-hosted Windows + NVIDIA runner** |
| Isolation tests | The escape suite (`provider-agent/tests/escape/`) run against the Windows runtime; plus Windows-specific probes: read `C:\`, reach Windows interop, reach the host LAN | Self-hosted runner |
| Parity test | Same Kit, same inputs, same image digest, on a Linux and a Windows host: output hashes equal where the Kit is deterministic | Both self-hosted runners |
| Soak | 24 hours with yield, sleep and reboot events injected | Self-hosted runner |
| Installer tests | Install, upgrade, rollback, uninstall leave no residue | Clean Windows VMs |

---

## 9. Owner decisions this plan waits on

| Decision | Blocks | Recommendation |
|---|---|---|
| Docker Desktop vs Docker Engine in the managed distro | W3 | Engine in the managed distro: no per-seat licence dependency, one controlled environment |
| Installer technology (MSI, MSIX, winget only) | W6 | MSI plus winget |
| Code-signing certificate and who holds it | W2, W6 | Standard Authenticode certificate; key in an HSM or hosted signing service |
| Minimum Windows version and NVIDIA driver version | W3, W14 | Supported Windows 10 and 11 releases that support WSL2 GPU compute; confirm exact numbers against NVIDIA docs |
| AMD on Windows go or no-go | W7 | Decide after the vendor-support check; default no-go |
| Privacy tiers on Windows hosts go or no-go | W12 | Default no-go; revisit with a Windows Server plus VM design |
| Multi-node on Windows hosts | W8 | Default no-go until networking is proven |
| Foreground-yield defaults, schedules, VRAM reserve | W5 | Conservative: yield on by default |
| Payout policy for Windows hosts (same as Linux?) | none | Same rate, since only the container tier is offered |
| Self-hosted Windows GPU runner hardware | W15 | One RTX-class machine on a clean Windows install |
| Windows VM tier (U14) go or no-go | W13 | Unchanged: owner's decision |

---

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| WSL2 GPU behaviour differs from native Linux (device selection, memory, performance) | wrong results or slow jobs | Parity test (section 8); capability flags honest; benchmark before listing a host |
| Owner's gaming and our jobs fight for the GPU | angry hosts, throttled jobs | X13 default yield, VRAM reserve, clear status in the tray and console |
| WSL2 memory and disk growth | host slowdown | Caps, reclaim, disk quota, health checks |
| Antivirus flags the supervisor or the runtime as miner-like | installs fail, trust loss | Signed binaries, vendor submission, no mining behaviour, transparent consent screen |
| Windows Home vs Pro differences | support burden | Requirements checker states exact needs; test both |
| A bug lets a job reach Windows files | host compromise | X6 hardening, isolation tests on every release, independent review (W11) |
| Updater bug bricks hosts | fleet loss | Rings, rollback, never update mid-job, forced-update floor only for security |
| Privacy claims leak to Windows hosts | false assurance | X5 and X4: capability flags gate the scheduler; tests assert Windows hosts never receive private jobs |
| All new code unrun (inherited from earlier plans) | defects stack | Run the existing suites first; W15 before enabling real hosts |

---

## 11. Master checklist

Tick an item only with its proof written after it. Items marked **(hardware)** need a recorded run on real machines.

### W0 Platform Abstraction Layer
- [ ] W0.1 PAL interfaces exist with Linux, Windows and fake implementations. *Proof: files + tests.*
- [ ] W0.2 No `os.getuid`, `os.mkfifo`, `/proc`, `/dev` or `/sys` access outside the PAL; an X8 lint test fails if one is added. *Proof: test + bug re-insertion.*
- [ ] W0.3 `psutil` is a declared dependency; RAM and CPU detection work without `/proc`. *Proof: pyproject + test.*
- [ ] W0.4 Linux behaviour unchanged: the full agent suite has the same result before and after. *Proof: counts.*

### W1 Capability advertisement
- [ ] W1.1 Heartbeat carries `platform` and `capabilities`; the server drops unknown flags. *Proof: tests.*
- [ ] W1.2 Scheduler refuses a host lacking a required flag; no default grants a flag. *Proof: tests.*
- [ ] W1.3 Windows hosts never receive private-tier, BYOC, gVisor or microVM jobs. *Proof: tests (X5).*
- [ ] W1.4 Admin fleet view shows platform and flags. *Proof: web test + screenshot.*

### W2 Windows supervisor
- [ ] W2.1 Runs as a Windows Service, starts on boot, stops gracefully. *Proof: Windows test (hardware or VM).*
- [ ] W2.2 Credentials in DPAPI or Credential Manager; none in plain text. *Proof: test + file scan (L7).*
- [ ] W2.3 Enrolment works on Windows. *Proof: recorded run.*

### W3 Managed runtime distro
- [ ] W3.1 Pinned rootfs imported by digest; hash mismatch aborts. *Proof: test.*
- [ ] W3.2 Hardening applied (automount, interop, PATH off; non-root; no SSH; caps). *Proof: config file + isolation tests.*
- [ ] W3.3 Docker Engine and NVIDIA toolkit installed at pinned versions. *Proof: provisioning log.*
- [ ] W3.4 GPU smoke test passes before enrolment completes. *Proof: recorded run (hardware).*
- [ ] W3.5 Repair and reset work without losing credentials. *Proof: test.*

### W4 Control channel
- [ ] W4.1 Versioned messages; token-authenticated; loopback only; no LAN listener. *Proof: test + port scan.*
- [ ] W4.2 `drain` finishes the current job then stops claiming; `stop` stops safely. *Proof: tests.*
- [ ] W4.3 Native GPU telemetry reaches the heartbeat. *Proof: test + recorded run (hardware).*

### W5 Resource governance
- [ ] W5.1 Foreground or full-screen GPU use stops new claims; schedules honoured. *Proof: tests with fake probe.*
- [ ] W5.2 Sleep blocked only while a job runs; reboot-pending drains and reports `maintenance`. *Proof: tests.*
- [ ] W5.3 VRAM reserve, disk and memory caps enforced. *Proof: tests + recorded run.*

### W6 Installer and updates
- [ ] W6.1 Signed installer; signature verified on install. *Proof: signature check output.*
- [ ] W6.2 Requirements checker gives a clear pass or fail list. *Proof: tests on clean VMs.*
- [ ] W6.3 Upgrade, rollback and uninstall leave no residue (service, distro, rules, credentials). *Proof: installer tests.*
- [ ] W6.4 Update manifests signed and verified; rollback on failed health check. *Proof: tests (L5 re-insertion).*

### W7 GPU features
- [ ] W7.1 Native NVML thermal cap and hard stop work on Windows. *Proof: test + recorded run (hardware).*
- [ ] W7.2 Multi-GPU device selection verified inside WSL2. *Proof: recorded run (hardware).*
- [ ] W7.3 AMD on Windows go or no-go recorded, with the vendor doc URL and date. *Proof: decision note.*

### W8 Networking
- [ ] W8.1 Egress allowlist proxy works inside the distro; blocked attempts logged. *Proof: opt-in Docker test on Windows.*
- [ ] W8.2 Multi-node decision recorded; `multi_node` flag stays off until proven. *Proof: decision note.*

### W9 Customer side
- [ ] W9.1 Python SDK test suite passes on Windows. *Proof: CI run.*
- [ ] W9.2 TypeScript SDK tests pass on Windows Node. *Proof: CI run.*
- [ ] W9.3 Browser flows (upload, wizard, session proxy) pass on Windows browsers. *Proof: recorded manual or automated run.*

### W10 Admin and operator
- [ ] W10.1 Rollout rings, pause and rollback work. *Proof: tests.*
- [ ] W10.2 Support bundle is redacted. *Proof: test + secret scan.*
- [ ] W10.3 Every operator script has a tested Windows path. *Proof: CI run.*
- [ ] W10.4 Metrics carry a `platform` label; Windows alert rules exist. *Proof: files + test.*

### W11 Security
- [ ] W11.1 Threat model written and reviewed. *Proof: doc + review.*
- [ ] W11.2 Isolation suite shows a job cannot read `C:\`, call Windows interop, or reach the LAN. *Proof: recorded run (hardware).*
- [ ] W11.3 Independent review of the supervisor, updater and installer. *Proof: report.*

### W12 and W13 Research
- [ ] W12.1 Privacy tiers on Windows: decision document with options and a go or no-go. *Proof: doc.*
- [ ] W13.1 Hyper-V VM runtime and U14 on Windows: decision document with a go or no-go. *Proof: doc.*

### W14 and W15 Docs and tests
- [ ] W14.1 Host guide, troubleshooting and website Windows section published. *Proof: pages.*
- [ ] W15.1 Windows CI jobs green; self-hosted GPU runner online. *Proof: CI run.*
- [ ] W15.2 Parity test: same Kit, same image digest, equal hashes on a Linux and a Windows host. *Proof: recorded run (hardware).*
- [ ] W15.3 24-hour soak with injected sleep and reboot events passes. *Proof: recorded run (hardware).*

### Sign-off
- [ ] S1 No change under `contracts/`, ledger, pricing, billing, settlement (X15, L3). *Proof: `git diff --stat`.*
- [ ] S2 Linux and Windows suites green; counts compared with the previous baseline. *Proof: outputs.*
- [ ] S3 Bug re-insertion run for every new check; restores verified by checksum. *Proof: recorded run.*
- [ ] S4 Every external interface recorded with doc URL and date (X11). *Proof: `docs/operations/external-interfaces-checked.md` updated.*
- [ ] S5 No secrets anywhere (L7). *Proof: scan.*
- [ ] S6 Docs and ADRs updated to match the code. *Proof: diff.*
- [ ] S7 Independent code review of the full diff. *Proof: report.*
- [ ] S8 Owner approval before any Windows host is enabled in production.

---

## 12. Master prompt

Copy everything inside the box into the agent.

~~~text
MASTER PROMPT: ComputeCoin cross-platform (Linux + Windows) work

ROLE
You are a senior engineer working in /home/root509/stocks. The plan is
docs/operations/cross-platform-windows-linux-master-plan.md. Read it completely, including the locked
decisions X1-X15 (section 2) and the inherited rules L1-L11 from
docs/operations/remaining-code-work-master-plan.md. If a decision looks wrong, STOP and ask the owner.

GROUND RULES
1. Work ONLY inside /home/root509/stocks. Never touch another project; never install anything system-wide.
   Tools go in the git-ignored .tools/ folder or project environments.
2. One agent at a time. Before EVERY edit, confirm the file's modification time is unchanged since you
   read it. If it changed, STOP and report.
3. Snapshot before the first edit of each package:
   git update-ref refs/snapshots/before-<id> $(git stash create "before <id>")
4. Follow the recommended order in section 7. One package at a time. W12 and W13 are research: write the
   decision document only, no production code.
5. Linux behaviour must not change. After W0, the agent, API and SDK suites must give the same results as before.
6. Never advertise a capability that is not proven (X4). Windows hosts never receive private-tier, BYOC,
   gVisor or microVM work (X5) until the owner approves a Windows design.
7. Verify every external interface (WSL2, Docker, NVIDIA, AMD, Windows APIs, installer tools) against
   current official documentation before using it; record URL and date next to the code (X11).
8. Every behaviour change has a test that fails without it; finish each package with bug re-insertion.
9. After each package run ALL suites (API, agent, Python SDK, TypeScript SDK, guardian and witness, e2e,
   web, contracts) on Linux, and the Windows jobs once W15 exists; compare counts with the previous baseline.
10. Tick a checklist item (section 11) ONLY with its proof written next to it. Hardware items need a recorded
    run from a real machine.
11. Never commit or push without the owner's instruction; if on main, branch first.
12. Do not edit contracts/, ledger, pricing, billing or settlement code.
13. Report in the section 13 format after each package, then WAIT.

TRAPS ALREADY HIT IN THIS CODEBASE
- Async SQLAlchemy cannot lazy-load: query, never touch relationship attributes on objects loaded
  without eager loading.
- Raising HTTPException rolls back the request's writes; return a JSONResponse when the failure itself
  must be recorded.
- Never read a security claim from an unsigned field next to signed bytes.
- Mocks follow the vendor's documented API, never the code's assumptions.
- Docs must match the code; never mark "done" what is not proven; use relative links.
- Keep test scratch inside the project (.tools/) or pytest's tmp_path; no network in tests.
- All code from the earlier plans was written without being run: expect import cycles and wrong names;
  run the existing suites before building on them.

WINDOWS TRAPS TO AVOID
- os.getuid, os.mkfifo, os.chmod permission semantics, /proc, /dev, /sys, signal handling and
  add_signal_handler differ or do not exist on Windows: keep them inside the Platform Abstraction Layer.
- Never build paths by string concatenation; in-container paths are PurePosixPath.
- Windows file permissions are not Unix modes: never rely on 0600 for secrets; use DPAPI.
- WSL2 shares one VM per user and its limits are per user: use a dedicated distro and caps.
- Bind mounts across the Windows/Linux boundary are slow and leak files: keep job data inside the distro.
- Never let a job reach a Windows drive, the Windows PATH or Windows interop (X6).
~~~

---

## 13. Report format (after each package)

| Section | Content |
|---|---|
| Files changed | One line each on why |
| Tests added | Names, and the failure each one pins |
| Suites | Before and after counts for every suite, on Linux and (once available) Windows |
| Bug re-insertion | Each check removed, the test that failed, restore verified by checksum |
| Checklist | Items ticked with proof; items not done and why |
| External docs | URL and date for every interface used (X11) |
| Decisions | Any owner decision needed before the next package |
| Risks | What is still uncertain and what hardware would settle it |
