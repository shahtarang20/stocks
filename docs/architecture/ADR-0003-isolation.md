# ADR-0003: Customer workload isolation

- Status: Accepted
- Date: 2026-10-16
- Phase: 0

## Context

ComputeCoin runs **untrusted customer workloads** on **third-party provider
hardware**. Both directions are hostile:

- a malicious **customer** workload may try to escape the sandbox, attack the
  host, steal other tenants' data, or pivot into the provider's network;
- a malicious **provider** may try to snoop on or tamper with a customer's code,
  data, or results.

This ADR covers the first direction (isolating the workload from the host and
from other tenants). The second direction (confidentiality from the provider)
is addressed by confidential-computing/attestation in a later phase and noted
under Residual Risk.

## Decision

### Phase 1 baseline — hardened containers

Run each workload in a container with restrictive settings:

- **No Docker socket** mounted into the container, ever. (A mounted
  `docker.sock` is root on the host.)
- **No host filesystem mounts**; no host credentials, env secrets, or cloud
  metadata endpoints reachable.
- **Non-root** inside the container; **drop all Linux capabilities** and add back
  none by default; `no-new-privileges`.
- **Seccomp** (default-deny-ish profile) and, where available, **AppArmor/SELinux**
  confinement.
- **Read-only root filesystem** with explicit, size-capped writable scratch.
- **Restricted egress**: deny by default; allow only what a job needs (e.g. the
  artifact store), block the provider's LAN and cloud metadata IPs
  (169.254.169.254).
- **Resource quotas**: CPU, memory, GPU, PIDs, and disk limits enforced by
  cgroups; time limits enforced by the scheduler.
- **GPU isolation**: dedicate GPUs per tenant (no sharing a device across
  tenants in Phase 1) and reset/clear device memory between jobs.

The **provider-agent does not execute jobs in this skeleton** (see
`provider-agent/computecoin_agent/security.py`). When job execution lands, the
agent must enforce the controls above and never:

- execute arbitrary customer shell commands on the host,
- mount host FS / docker socket / host creds into workloads,
- run privileged containers.

### Path to stronger isolation

Ordinary containers share the host kernel, so a kernel exploit breaks the
boundary. For stronger guarantees we plan, in order of increasing strength:

1. **gVisor / user-space kernel** — shrinks the host-kernel attack surface.
2. **microVMs** (Firecracker / Cloud Hypervisor / Kata Containers) — per-workload
   VM with its own kernel; much stronger host isolation.
3. **Dedicated hosts / bare-metal per tenant** — strongest; no co-tenancy.
4. **Confidential computing** (SEV-SNP / TDX / confidential GPUs) + remote
   attestation — protects the customer **from the provider**, and underpins
   future "VERIFIED" capability claims.

## Explicit statement

**Ordinary containers do not guarantee complete isolation from malicious
workloads.** They share the host kernel; a kernel or runtime vulnerability can
be used to escape. Container hardening raises the bar and is our Phase-1
baseline, but strong isolation guarantees require microVMs or dedicated hosts.
We state this plainly to customers and providers rather than implying containers
are a security boundary they are not.

## Residual risk

- Kernel-level escapes remain possible on the container baseline until microVMs
  ship.
- Side-channels (e.g. across shared caches/GPU) are not fully mitigated by
  containers.
- Provider-side confidentiality is **not** provided until confidential computing
  lands; until then, customers should treat provider hosts as able to observe
  workloads, and we must disclose this.

## Consequences

- Scheduler and agent must encode and enforce these settings; a job that cannot
  be placed under them is rejected, not downgraded.
- We market isolation honestly (baseline vs strong) and gate sensitive
  workloads to stronger tiers as they become available.

---

## Addendum: AMD GPU Support (ROCm) — D5 Tier Matrix & D6 Device Rules

### D5 Isolation Tier Matrix for AMD GPU Workloads

In v1, AMD GPU workloads run strictly under the **container** isolation tier:

| Isolation Tier | NVIDIA GPU | AMD GPU (ROCm) | CPU (Simulated) |
| :--- | :--- | :--- | :--- |
| `container` | Supported | **Supported (v1)** | Supported |
| `gvisor` (`runsc`) | Experimental | **Refused (422 / IsolationUnavailableError)** | Supported |
| `microvm` (Kata / Firecracker) | Planned (VFIO) | **Refused (422 / IsolationUnavailableError)** | Supported |
| `sealed` / `sealed_pro` / `confidential` | Host-memory encryption | **Refused (v1)** | Supported |

Rationale: Kata Containers and gVisor do not have battle-tested, upstream-supported ROCm KFD + render-node virtualization paths in our production stack. Silently downgrading an isolation tier is forbidden: any AMD GPU job requesting non-container isolation is rejected at validation (`validate_job` -> 422), and at the agent (`IsolationUnavailableError`).

### D6 Device Passthrough Rules for AMD GPUs

AMD GPU compute requires access to the kernel fusion driver and direct render nodes. To maintain isolation parity with ADR-0003:

1. **Explicit Device Nodes**:
   - Passthrough `/dev/kfd` with `--device /dev/kfd:/dev/kfd:rwm`.
   - Passthrough only the specific assigned render node: `--device /dev/dri/renderD<N>:/dev/dri/renderD<N>:rwm`.
   - Never mount `/dev/dri` wholesale.
   - Never mount `/dev/dri/card*` (display engine / modesetting is irrelevant to compute).
2. **Numeric Group Identification**:
   - Query host GIDs directly from the device nodes: `stat -c %g /dev/kfd` and `stat -c %g /dev/dri/renderD<N>`.
   - Pass `--group-add` for each distinct GID that owns `/dev/kfd` and the assigned render nodes (usually `render`, sometimes `video`). Never hardcode a GID such as 44 or 107.
3. **Preserved Hardening Invariants**:
   - `--cap-drop ALL`
   - `--security-opt no-new-privileges:true`
   - `--read-only` rootfs
   - `--network none` (unless explicit egress allowlist)
   - Dedicated writable tmpfs and scratch directory.
4. **Forbidden Flags**:
   - Never use `--privileged`.
   - Never use `--ipc=host` or `--pid=host`.
   - Never use `--security-opt seccomp=unconfined`.
   - Never use `--cap-add` for compute access.
