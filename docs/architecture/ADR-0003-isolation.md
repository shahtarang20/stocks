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

By default, AMD GPU workloads run under the **container** isolation tier. `sealed` / `sealed_pro` are coded but OFF by default behind three locks (addendum A6 below); the table shows the default:

| Isolation Tier | NVIDIA GPU | AMD GPU (ROCm) | CPU (Simulated) |
| :--- | :--- | :--- | :--- |
| `container` | Supported | **Supported (v1)** | Supported |
| `gvisor` (`runsc`) | Experimental | **Refused (422 / IsolationUnavailableError)** | Supported |
| `microvm` (Kata / Firecracker) | Planned (VFIO) | **Refused (422 / IsolationUnavailableError)** | Supported |
| `sealed` / `sealed_pro` | Host-memory encryption | **Refused by default; coded but off behind three locks (A6)** | Supported |
| `confidential` | Host-memory encryption | **Refused (no AMD GPU confidential mode)** | Supported |

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

### Addendum A6 (P6): Sealed and Sealed Pro on AMD (code written, owner decision D5 PENDING)

Code now allows `sealed` and `sealed_pro` for ROCm in `app/services/gpu_runtime.py::is_gpu_compatible`, and the submit check in `scheduler.validate_job` lets those two tiers through to it, **only when the machine's attestation verdict names a Sealed image** (`image_hash` set and `tier_reached` in `sealed`/`sealed_pro`). Sealed and Sealed Pro rest on the TPM and the Sealed OS, which do not depend on the GPU vendor. A machine with no such verdict is still refused with `amd_isolation`.

Still refused on ROCm: `confidential` (it needs a GPU TEE, which current AMD GPUs do not have; a hardware limit, not a code task), `gvisor` and `microvm`.

This changes the D5 matrix row above (`sealed` / `sealed_pro`: Refused). **The owner has not yet decided D5.** Until they do, treat the matrix above as the policy of record, and do not advertise AMD Sealed to customers. The machine-level verdict is persisted on `provider_machines.sealed_verdict` (migration `0040_sealed_verdict`, written when a non-job-bound Sealed verdict is issued) and `is_gpu_compatible` additionally requires the owner flag `AMD_SEALED_ENABLED` (default `false`), so AMD Sealed stays refused until the owner flips D5 and a real AMD + TPM host has been attested (P4/P5). The agent adds a third lock: the host must set `amd_sealed_enabled = true` in its own agent config (default `false`, `provider-agent/computecoin_agent/config.py`), otherwise the executor raises `IsolationUnavailableError`. None of this has run on real AMD hardware.

### Addendum U13: multi-node group network (code written, never run; OWNER DECISION before `MULTI_NODE_ENABLED`)

Multi-node ranks need a network path for NCCL/rendezvous across hosts. This is the only place `--network none` is
replaced, and only for a job whose claim carries `rendezvous`:

- The operator creates one **non-internal** user-defined docker network per datacenter group (bridge, or macvlan/overlay)
  with `scripts/setup-multinode-net.sh` and names it in the agent config `multinode_network`. It is not `--internal`
  because Docker ignores `--publish` on internal networks and rank 0 must publish (U13.1). Confinement is by HOST
  FIREWALL rules the same script installs (nftables preferred, iptables DOCKER-USER fallback), each tagged with the marker
  comment `computecoin-multinode:<network>`: forward traffic from the group subnet is dropped unless the destination is
  inside the group subnet or the private cluster range, and `169.254.169.254` (metadata) is always dropped, so there is
  no route to the internet, to other private networks or to the egress proxy network. The script refuses to create the
  network when it cannot install the rules, and `--verify` prints OK/FAIL (network not internal and rules present).
  The agent never creates or changes the network or the rules itself.
- Agent gate: the operator sets `multinode_network_firewalled = true` (attestation, default false) after `--verify`
  printed OK. A rendezvous job is refused with `multinode_firewall_not_attested` without it, and with
  `multinode_firewall_check_failed` when the runtime self-check (`executor.check_multinode_firewall`: `docker network
  inspect` shows Internal=false and the rule marker is present in nftables or DOCKER-USER) fails.
- The container is attached to that one network: `--network <multinode_network>` instead of `--network none`. Nothing
  else changes: `--read-only`, `--cap-drop ALL`, `no-new-privileges`, non-root user, pids/memory/cpu limits, tmpfs, private
  IPC with bounded `--shm-size`, pinned image digest, isolation tier rules. No `--cap-add`, `--privileged`, `--ipc=host`,
  `--pid=host`, host network or published ports (other than the rank-0 rendezvous publish of U13.1) are introduced.
- Fail closed in the agent: a rendezvous job on a host without `multinode_network` (or without the firewall attestation
  and a passing self-check) is refused; a rendezvous job that also
  declares an egress `network_policy` is refused; a rendezvous job on `sealed`/`sealed_pro`/`confidential` is refused (and
  the API refuses it at validation/submit with a field error).
- Impact on the isolation claim: containers of one group can talk to each other (and to anything else attached to that
  network on the same host) on every port. They cannot reach the internet while the firewall rules hold (if the rules are flushed the network is routable: the
  self-check runs before every rank start). A malicious customer job can therefore probe
  the other ranks of its own group, which are its own jobs. Use one network per group and never attach other
  workloads to it. Cross-customer exposure exists only if the operator reuses one network for several groups: do not.
- Residual risk: overlay/macvlan drivers add kernel/network-stack attack surface and, for overlay, traffic is
  unencrypted unless the owner enables encryption. Whether the chosen driver makes the rank-0 container reachable at the
  rank-0 host's `cluster_addr` (the `MASTER_ADDR` we hand out) is NOT verified and is part of the owner decision.

### Addendum U13.1: rank-0 reachability (supersedes "no published ports" for multi-node rank 0 only)

Decision: only the rank-0 container of a multi-node group gets `--network-alias cc-rank0-<job>` and
`--publish <cluster_addr>:<master_port>:<master_port>`, where `cluster_addr` is the host's configured literal private
address and must equal the `master_addr` the API handed out. The publish is bound to that address only, never 0.0.0.0, a
public or a loopback address; the agent refuses the job on any mismatch, a non-private `master_addr`, or a missing
`cluster_addr`. Other ranks publish nothing. All other hardening (no egress, no privileged flags, tier refusals) is unchanged.
Risks: a published port makes the rendezvous port of that container reachable by every host that can route to the private
cluster network, so that network must be a closed datacenter segment; Docker ignores `--publish` on `--internal`
networks, so the group network is a non-internal bridge/macvlan/overlay with egress blocked by the host firewall rules of
`scripts/setup-multinode-net.sh` (required, attested and self-checked; not verified on real hosts); NCCL data ports between ranks are not covered by this publish.
