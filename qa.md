# ComputeCoin — Comprehensive QA & Architectural Edge Cases Playbook

**Document Version:** 1.0  
**Target Environment:** Production Distributed AI Compute & DePIN Marketplace  
**Audience:** QA Engineers, System Architects, Security Auditors, Node Operators  

---

## Executive Summary

ComputeCoin operates at the intersection of **untrusted consumer/enterprise hardware**, **untrusted client code**, **distributed networking**, and **financial settlement**. Unlike centralized clouds (AWS, GCP) where the hypervisor owns all hardware, a decentralized compute marketplace must assume that:
1. **Hosts may crash, overheat, drop packets, or behave maliciously.**
2. **Clients may submit exploit payloads, infinite loops, or attempt balance double-spends.**
3. **The network between nodes is unreliable and subject to latency and partitions.**

This document provides definitive architectural answers to the user-raised edge cases, followed by an exhaustive catalog of real-world edge cases, failure modes, mitigations, and QA test protocols.

---

# Part 1: Detailed Analysis of the 8 Core Edge Cases

### 1. Can One Server / Host Provide One GPU to Multiple Users?

#### Current Architecture (Phase 1 Baseline)
* **Dedicated Mode (`max_concurrent_jobs: 1`):** A physical GPU is allocated exclusively to one user per time slot. No device sharing between tenants is permitted.
* **Security Rationale:** Prevents cross-tenant CUDA memory snooping, cache timing side-channels, and out-of-memory crashes caused by neighbor processes.

#### Production Solution for Multi-Tenancy (Phase 2+)
To safely slice a single GPU across multiple clients without cross-talk:
1. **NVIDIA MIG (Multi-Instance GPU) on Enterprise Hardware (A100 / H100):**
   * Hardware-level partitioning splits one physical GPU into up to **7 isolated GPU instances** (e.g. 7x 1g.10gb instances).
   * Each instance features dedicated compute engines, memory controllers, and isolated VRAM. A crash in Instance 1 has zero impact on Instance 2.
2. **NVIDIA MPS (Multi-Process Service) / vGPU:**
   * Used for concurrent inference jobs on consumer hardware (RTX 4090 / 3090).
   * **Mandatory Security Requirement:** The agent must issue a device reset (`cudaDeviceReset()` / `cudaMemset`) between client switches to zero out residual GPU buffer memory.

---

### 2. How Do We Verify If a Provider System Is Genuinely Capable? (Hardware Attestation)

#### The Problem: Spoofing & Fraud
A malicious host can modify `/proc/cpuinfo`, wrap `nvidia-smi` in a script, or modify `sysfs` to report an "NVIDIA H100 80GB" when running an old GTX 1060 or a CPU emulator.

#### Multi-Tier Verification Engine
1. **Low-Level Native Probes (Implemented):**
   * The `computecoin-agent` queries NVML (NVIDIA Management Library) and PCIe device IDs directly via binary syscalls, bypassing easily falsified shell wrapper scripts.
2. **Honesty Invariant (`DETECTED` vs `VERIFIED`):**
   * As documented in `ADR-0003` and the codebase, self-reported hardware is strictly tagged as **`verification_status: "detected"`**. It is **never** shown as `verified` on the UI.
3. **Synthetic Micro-Benchmark Probe (Phase 2):**
   * When a node first enrolls, the control plane dispatches an automated 15-second benchmark container:
     - **FP16 / FP8 Tensor Core GEMM Test:** Measures real TFLOPS.
     - **PCIe Host-to-Device Transfer Test:** Verifies memory bus width (GB/s).
     - **VRAM Allocation Stress:** Confirms physical addressable RAM by allocating 95% of declared VRAM.
   * If measured throughput deviates by $>25\%$ from authentic hardware envelopes, the machine is flagged as `unverified` and excluded from high-tier search rankings.

---

### 3. Can a Host Decide to Provide One GPU to Multiple Users?

#### Yes — Operator Controlled Capacity Slots
1. **The Provider Dashboard Setting:**
   * In `apps/api/app/api/routers/providers.py` (`PATCH /provider/machines/{id}`), the provider controls `max_concurrent_jobs`:
     ```json
     {
       "max_concurrent_jobs": 4,
       "supported_workloads": ["batch-inference", "text-generation"]
     }
     ```
2. **Atomic Capacity Invariant:**
   * As defined in `app/services/marketplace.py`:
     $$\text{Used Slots} = \sum_{\text{active}} \text{reservations.slots}$$
   * The engine enforces that a new reservation of $S$ slots is granted **if and only if**:
     $$\text{Used Slots} + S \le \text{max\_concurrent\_jobs}$$
   * If a host designates `max_concurrent_jobs: 1`, the entire GPU is dedicated exclusively to one user until release.

---

### 4. What Happens If a Host Crashes or Powers Off Mid-Job? Can the Client Failover?

#### The Recovery Sweeper Engine (Already Implemented in `app/services/leases.py`)
1. **Active Lease TTL:** Every running job carries a `lease_expires_at` timestamp in PostgreSQL, renewed every 30 seconds by agent heartbeats.
2. **Automatic Detection:** If the host turns off, heartbeats cease. Within 90 seconds (`ONLINE_WINDOW_SECONDS`), the recovery sweeper marks the lease expired.
3. **Automatic Failover & Requeue:**
   * If `job.attempts_made < job.max_retries`:
     - The job transitions: `RUNNING` $\rightarrow$ `RETRY_PENDING` $\rightarrow$ `QUEUED`.
     - The scheduler automatically dispatches the job to a **second healthy host** with matching hardware specifications.
4. **Financial Guarantee:**
   * Held funds are never captured. If retries are exhausted, the job transitions to `TIMED_OUT`, and 100% of held credits are returned to the client balance immediately.

---

### 5. Health Notifications Between Client and Host

#### Architecture for Real-Time Status Alerts
1. **Telemetry Streaming:**
   * The agent reports hardware metrics (`gpu_temp_c`, `gpu_util_pct`, `mem_used_mb`, `uptime_s`) on every heartbeat.
2. **Notification Triggers:**
   * **Host Side:** If temperature exceeds 80°C or memory reaches 90%, the provider receives an email/UI alert: *"Node root509 is experiencing high thermal load."*
   * **Client Side:** If a host drops connection, a WebSocket/UI event displays: *"Host node disconnected ungracefully. Job rerouting to secondary node..."*
   * **Post-Mortem Log:** An entry is written to the audit log (`app.models.audit`) with reason `node_offline_unresponsive`.

---

### 6. Client/Host Feedback & Reputation-Based Search Ranking

#### Dynamic Ranking Engine
1. **Post-Job Rating:**
   * Clients rate completed jobs (1–5 stars) with objective metrics: execution speed, log clarity, uptime.
2. **Algorithmic Ranking Score:**
   Instead of a static list, marketplace search ranking is dynamically calculated:
   $$\text{Rank Score} = (0.35 \times \text{Uptime \%}) + (0.35 \times \text{Job Completion Rate}) + (0.20 \times \text{User Rating}) - (0.10 \times \text{Price Ratio})$$
3. **Automatic Demotion & Delisting:**
   * A host that ungracefully drops 3 jobs in a 24-hour period has its rank slashed by 50%.
   * If reliability drops below 80%, the node is automatically hidden from public marketplace search until re-benchmarked.

---

### 7. Second Host Failover in Disasters (Warm Standby & Checkpoints)

#### Checkpointing Protocol
For long-running AI training or heavy rendering tasks:
1. **Periodic Artifact Sync:**
   * Workloads are configured to save checkpoint files (e.g. `checkpoint_step_1000.pt`) to cloud object storage (Supabase/S3).
2. **Seamless Recovery:**
   * When Host A suffers a power outage, Host B is assigned the job.
   * Host B downloads the latest checkpoint from S3 and resumes training from step 1,000 rather than step 0, saving hours of wasted compute.

---

### 8. Can a Heavy Client Job Destroy or Burn Out the Host's Laptop/Server?

#### Hardware Protection & Guardrails
Without controls, an intense AI workload could cause thermal runaway, kernel panic, or storage exhaustion. ComputeCoin implements four layers of hardware defense:

1. **Thermal Throttling & Power Limits:**
   * The agent checks GPU core temperature before accepting jobs. If $T > 82^\circ\text{C}$, the node sets availability to `maintenance` and rejects new jobs.
   * The agent can enforce maximum GPU TDP via `nvidia-smi -pl <watts>`.
2. **Cgroups v2 Resource Quotas (ADR-0003):**
   * Memory limits (`--memory=28g --memory-swap=28g`) prevent client containers from consuming 100% of host RAM and triggering the Linux Out-Of-Memory (OOM) killer on the host OS.
3. **Disk Write Caps:**
   * Containers mount a size-capped writable scratch space (e.g., `--storage-opt size=30G`). A client attempting to generate 500GB of junk files will receive `ENOSPC` (No space left on device) without filling the host's physical hard drive.
4. **PID / Thread Limits:**
   * `--pids-limit 512` neutralizes malicious fork-bombs and runaway multiprocessing scripts.

---

# Part 2: 12 Critical Real-World QA Edge Cases for DePIN GPU Networks

Below is an exhaustive catalog of failure modes, vulnerabilities, and QA edge cases identified from real-world distributed compute infrastructure:

| # | Edge Case Category | Specific Failure Scenario | Impact | Mitigation Strategy |
|---|---|---|---|---|
| **E1** | **Security / Sandbox** | Malicious client runs container breakout (CVE-2024-0132 / dirty pipe) | Host machine compromised, host files stolen | Rootless containers, drop all capabilities (`--cap-drop=ALL`), seccomp default-deny, no docker.sock access |
| **E2** | **Privacy / IP Theft** | Malicious host snoops on customer model weights or prompts in VRAM | Intellectual property theft | Encrypted checkpoint artifacts, confidential computing (SEV-SNP / TDX / H100 CC) |
| **E3** | **Network Abuse** | Client runs DDoS attack, torrents, or spam from host's residential IP | Host ISP blocks or terminates provider's internet | Strict egress firewall: allow only S3/HuggingFace IPs, block port 25, block private LAN (192.168.x.x, 10.x.x.x) |
| **E4** | **Metering Fraud** | Host throttles clock speed to 10% to bill 10 hours for a 1-hour job | Client overcharged for degraded compute | Expected tokens/sec benchmark bounds; maximum job wall-clock timeouts; client approval gates |
| **E5** | **Timing / NTP Drift** | Host alters system clock by +2 hours to simulate longer runtime | Financial discrepancy in billing | Control plane is authoritative for all timestamps (`utcnow()`); agent clock drift rejected |
| **E6** | **Financial Race Condition** | Client with $10 submits five $10 jobs concurrently across tabs | Account balance drops to -$40 (negative balance) | Atomic `SELECT FOR UPDATE` on billing account row; single-transaction reserve + ledger hold |
| **E7** | **Disk Space Exhaustion** | Completed job creates a 100GB model weight file, filling host disk | Host filesystem crashes, agent cannot log | Automated post-job disk cleanup; dedicated ephemeral partition; size-quota checks |
| **E8** | **Zombie Reservations** | Client requests quote and reserves slot, but never starts the job | GPU remains blocked, provider earns nothing | Hard 300-second TTL on reservations; auto-release sweeper frees capacity if unqueued |
| **E9** | **Partial Job Interruption** | Job runs for 55 minutes of a 60-minute task and host loses power | Dispute over whether provider should be paid | Prorated compensation based on verified intermediate checkpoints; unmetered portion refunded |
| **E10** | **On-Chain Settlement Front-Running** | Malicious actor monitors mempool to claim another user's settlement | Token theft on Sepolia/Base | Key smart contract escrows by `keccak256(customer, jobId)`; verify EIP-712 signer |
| **E11** | **Network Split-Brain** | Host can communicate with customer container but loses connection to API | Control plane thinks node is dead; host still running job | Container watchdog: if agent misses API communication for 120s, container is paused |
| **E12** | **CUDA Version Incompatibility** | Client submits PyTorch 2.4 / CUDA 12.4 job to host with CUDA 11.8 driver | Workload crashes on launch with driver error | Workload specification matching: scheduler matches jobs only to nodes with driver $\ge$ required version |

---

# Part 3: QA Test Matrix & Execution Guide

### Suite 1: Concurrency & Invariant Verification
```bash
# Verify double-entry ledger balance conservation and non-negative constraints:
python tests/end-to-end/phase5_billing_e2e.py

# Verify atomic capacity locking under concurrent multi-worker load:
python tests/end-to-end/phase3_concurrency_e2e.py
```
* **Expected Result:** Exactly 1 reservation succeeds, rest return `409 Conflict: no_capacity`. No double-booking.

### Suite 2: Recovery Sweeper & Lease Expiry
```bash
# Verify stuck jobs and disconnected agents are automatically re-queued:
pytest apps/api/tests/test_phase4_jobs.py -k "test_recover_expired_leases"
```
* **Expected Result:** Unresponsive hosts are unassigned, capacity is restored, and client credits are returned.

### Suite 3: Security & Isolation Verification
```bash
# Verify that non-root, capability drop, and credential isolation are enforced:
pytest apps/api/tests/test_phase8_isolation.py
```
* **Expected Result:** All privilege escalation and host filesystem access attempts return `PermissionDenied`.

---

# Part 4: Production Pre-Launch Safeguard Checklist

- [x] **Double-Submit CSRF & Bearer Dual Authentication** active across API and Web.
- [x] **Non-Negative Balance Constraints** enforced in PostgreSQL DDL (`CHECK (balance_minor >= 0)`).
- [x] **Automatic Lease Recovery Sweeper** enabled in API lifespan (`_recovery_sweeper()`).
- [x] **SameSite=None; Secure** cookie attributes deployed for cross-origin production.
- [x] **Smart Contract Escrow Safety** verified with 12 passing Hardhat tests.
- [ ] **Hardware Thermal Capping** configured on provider agent daemon.
- [ ] **Egress Whitelist Firewall** configured on provider container runtime.
