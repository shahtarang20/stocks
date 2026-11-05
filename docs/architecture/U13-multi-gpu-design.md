# U13 design note: multi-GPU on one host and datacenter multi-node

Status: code written, never run or tested (network, failure propagation, tier refusal, benchmark wiring and billing note added in the follow-up). Needs owner approval (L8) before it is switched on. Relates to ADR-0006
section 7 (Multi-GPU, Multi-node), WP-U13, ADR-0003 (isolation limits), U7 (scheduler v2).

## 1. Multi-GPU on one host (U13.1)

- A job needing N GPUs needs ONE host with N GPUs (`scheduler_v2.machine_fit_reason`, unchanged). It is never split.
- The agent picks the device set: `gpu_alloc.GpuAllocator` hands out the N lowest free GPU indices, held until the job
  ends, so two jobs never share a card. Docker gets `--gpus "device=0,1,2,3"` (literal inner quotes; to verify against
  current Docker/NVIDIA Container Toolkit docs, L4). One-GPU jobs keep the byte-identical `--gpus 1`.
- Shared memory: `--shm-size`, default a quarter of the job memory limit, at most half of it, at most 16 GiB
  (`limits.shm_mb` can lower or raise within those bounds). ADR-0003 stays intact: private IPC, never `--ipc=host`,
  no `--cap-add`, no `--privileged`. `--ulimit memlock` is NOT set; if NCCL needs it that is an owner decision under ADR-0003.
- NCCL environment on one host: `NCCL_DEBUG=WARN`, `NCCL_IB_DISABLE=1`, `NCCL_SOCKET_IFNAME=lo` (no network probing under
  `--network none`), and `COMPUTECOIN_GPU_COUNT`.
- AMD: not changed. The allocator is NVIDIA only; multi-GPU AMD jobs keep today's behaviour.

### Waiting rule

A gang job (v2 job with more than 1 GPU or more than 1 node) with no fitting host used to fail at the first sweeper pass
(`max_retries=0`). Now `scheduler.run_scheduler` leaves it QUEUED and does not count an attempt until
`MULTI_GPU_MAX_WAIT_SECONDS` (default 1800) has passed since the job was created; then it fails `no_provider` and the
hold is released as before. Unknown creation time counts as expired. v1 and single-GPU jobs are unchanged. The wait is
bounded; a job can never queue forever. Open: whether the wait should be a per-job option in the spec (owner).

## 2. Multi-node (U13.2), off by default

`MULTI_NODE_ENABLED=false`: a spec with `resources.nodes > 1` is refused at validation (`multi_node_disabled`).

### Which hosts qualify (OWNER DECISION)

An administrator sets `datacenter_group` on a machine: `PUT /v1/admin/machines/{id}/datacenter-group`
(`{"datacenter_group": "<label>"}`, `null` clears). Default none. A host without a label is never used for multi-node.
The owner decides which hosts and labels qualify and the thresholds below.

### Benchmark

1. Each labelled host runs a benchmark responder on its private address (`netbench.start_responder`) and reports its
   address. `GET /v1/agents/benchmark/plan` returns the peers of the same label with a stateless nonce.
2. The prober measures median round-trip latency and one-direction bandwidth to each peer, and sends them to
   `POST /v1/agents/benchmark/report`. The peer's answer carries `HMAC(peer signing key, nonce | prober)`; the API
   verifies it with the peer's key, so a host cannot invent a link to a peer that did not answer. A pair is stored only
   when nonce, label and proof check out; pairwise rows are directional (`network_benchmarks`).
3. Limits: nonce lifetime `MULTI_NODE_BENCHMARK_NONCE_TTL_SECONDS` (300), measurements older than
   `MULTI_NODE_BENCHMARK_MAX_AGE_SECONDS` (86400) are ignored. Proposed thresholds (owner to set): latency at most
   `MULTI_NODE_MAX_LATENCY_MS` (2.0 ms), bandwidth at least `MULTI_NODE_MIN_BANDWIDTH_MBPS` (10000), both directions of
   every pair. Report values are range checked; a self-reported `own_addr` must be a literal private address.

### Placement

`scheduler_v2.pick_node_group` takes the scheduler's ranked hosts, keeps those with a label, and returns the first label
(ordered by best-ranked member) where `nodes` hosts all pass every pairwise check, else None, and the job waits under the
rule above. Node count is capped by `MULTI_NODE_MAX_NODES` (4). Rank 0 is the customer's job. Every other rank is an
ordinary v2 job pinned to one host (`node_group_members`), created with `node_count=1`, with its own hold and billing:
no ledger, pricing or settlement code changes (L3). Consequence to confirm with the owner: the customer's budget and
`max_cost` apply per rank job, so a multi-node run costs N times the per-job hold.

### Rendezvous

The claim of a member job carries `rendezvous`: `master_addr` (the rank-0 host's `cluster_addr`), `master_port`
(`MULTI_NODE_MASTER_PORT`, 29500), `node_rank`, `nnodes`, `gpus_per_node`, `world_size`. The agent turns it into
`MASTER_ADDR`, `MASTER_PORT`, `NODE_RANK`, `NNODES`, `NPROC_PER_NODE`, `WORLD_SIZE`. A member whose master address is
missing is failed at claim (`rendezvous_unavailable`), never started.

### Billing: the run's max_cost is split across ranks

Every rank is its own job with its own hold, billed by the existing per-job pipeline (no ledger, pricing or billing
code changes, L3). For a multi-node job the spec's `billing.max_cost` (or the platform default
`MULTI_NODE_DEFAULT_MAX_COST` when absent) is the cap for the WHOLE run: each rank job gets `max_cost / node_count`
rounded DOWN to cents (`scheduler_v2.per_rank_max_cost`, minimum `MULTI_NODE_MIN_RANK_COST`), so the funds held across
ranks never exceed `max_cost`. `estimated_minutes` stays per rank (ranks 1..N-1 copy it; multiplying it would charge N
squared). `jobs_v2` stores the rank share on rank 0 and `multi_node.create_group` gives every sibling the same share.
`POST /v1/jobs/v2` exposes `spec_v2.hold_estimate = {per_rank_minutes, node_count, total_minutes, run_max_cost,
rank_max_cost}` (only when `nodes > 1`). A run whose per-rank hold exceeds its share is refused by the existing
`budget_exceeded` check. If the minimum per-rank amount exceeds cap/N the job is refused the same way.

### Network, failure propagation and tiers (code written, never run)

- Network: the agent attaches a rendezvous job to the operator-created NON-internal docker network named by the agent config
  `multinode_network` (`scripts/setup-multinode-net.sh`), whose egress is blocked by host firewall rules the script
  installs (marker `computecoin-multinode:<network>`, `--verify` prints OK/FAIL). Refused when unset, when
  `multinode_network_firewalled` is not true (`multinode_firewall_not_attested`) or when the runtime self-check fails
  (`multinode_firewall_check_failed`). ADR-0003 addendum U13 records
  the isolation impact. Not verified: that the chosen docker driver makes rank 0 reachable at the host `cluster_addr`
  that is handed out as `MASTER_ADDR`.
- Rank failure: `multi_node.sweep_rank_failures` (worker step, own try/except, only when `MULTI_NODE_ENABLED`) cancels the
  live sibling ranks when any rank failed, was cancelled or timed out, using the v1 cancel sequence (reservation
  released, FSM cancelled, hold released in full). The agent's job monitor polls the job heartbeat every 10 s; a 409
  (job no longer RUNNING) stops the container (`docker kill` of the named container, result `cancelled`).
- Tiers: `sealed`, `sealed_pro` and `confidential` are refused for `nodes > 1` (`multi_node_private_tier_refused`, field
  error on `isolation_tier` in `POST /v1/jobs/v2`, and in `scheduler.validate_job`). U18 does not cover multi-node.
- Benchmark: the agent `start` command runs the responder (asyncio, bound only to the configured `cluster_addr`) and the
  prober loop when the agent config has `netbench_enabled = true` (default false) and `cluster_addr` set.

### Known gaps and owner decisions

- The group network driver and the address mapping above (owner decision, L4 verification of docker flags).
- `jobrunner.poll_loop` creates one shared `GpuAllocator` when an NVIDIA host has more than one GPU, so concurrent multi-GPU jobs get disjoint
  `--gpus "device=..."` sets (detection failure means no allocator; AMD hosts do not use it). Not run on a real multi-GPU host.
- A cancelled rank's agent still tries to post its result: the API now refuses it with 409 `job_not_running` for a
  CANCELLED job without storing anything or touching billing (the hold was already released), and the agent treats the
  409 as final (no retry; summary status `cancelled`).

### Rank failure idempotency and rank-0 reachability (decision)

- Exactly once: `sweep_rank_failures` selects only `placed`/`running` groups under a row lock (`skip_locked`);
  `propagate_group_failure` now returns at once for a `done`/`failed` group and `_cancel_rank_job` does nothing for a job
  no longer cancellable, so siblings are cancelled once and `billing.release_hold` (ledger key `release_hold:<job_id>`,
  already idempotent, and a no-op for a settled job) releases each hold once.
- Reachability: `master_addr` is the `cluster_addr` of rank 0's host. On rank 0 the agent executor
  (`DockerExecutor._rank_network_args`) refuses unless the host's configured `cluster_addr` equals `master_addr`, then
  starts the container on the group network with `--network-alias cc-rank0-<job>` and
  `--publish <cluster_addr>:<port>:<port>`: published ONLY on the private cluster address, never 0.0.0.0. Other ranks
  publish nothing and only dial `master_addr:port`. Any rank whose `master_addr` is not a literal private address is refused.
- Risks (unverified, needs a real two-host test): Docker does not publish ports of containers attached only to an
  `--internal` network, so the group network is a non-internal bridge (or macvlan/overlay) whose egress is blocked by
  the host firewall rules; without those rules the container has a default route, hence the attestation and self-check. NCCL also opens ephemeral data ports between ranks, which `--publish` of the rendezvous port alone
  does not cover, so the network driver must allow rank-to-rank traffic on cluster addresses. Owner decision.

## 3. Data model (migration 0037_multi_gpu_node, revises 0036_services)

`provider_machines.datacenter_group`, `provider_machines.cluster_addr`; tables `network_benchmarks`, `node_groups`,
`node_group_members`.

## 4. Tests (written, not run)

`apps/api/tests/test_u13_multi_gpu_node.py`, `apps/api/tests/test_u13_sweep_cap.py`, `provider-agent/tests/test_gpu_alloc_u13.py`, `provider-agent/tests/test_multinode_net_u13.py`, `provider-agent/tests/test_netbench_firewall_u13.py`. Not covered: DB-level
benchmark storage, claim rendezvous, run_scheduler integration (needs the API test fixtures).
