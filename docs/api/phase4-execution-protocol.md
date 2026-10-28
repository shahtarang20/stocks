# Phase 4 — Job execution: FSM, scheduling, agent dispatch, artifacts (AUTHORITATIVE CONTRACT)

Single source of truth for Phase 4. Backend (`apps/api`), provider agent
(`provider-agent`), SDKs (`sdk/*`), web (`apps/web`) MUST conform exactly.
Builds on Phase 2 (machine HMAC auth) and Phase 3 (quotes/reservations/pricing).

JSON everywhere. Timestamps RFC 3339 UTC. Money = decimal strings (Phase 3).
MVP workloads: **`text-generation`** and **`batch-inference`** only.

## Honesty rules (enforced)
- `execution_mode` is `"simulated"` or `"hardware"`. The `SimulatedExecutor`
  (CPU dev, no GPU/Docker) produces a DETERMINISTIC transform and every job/output
  it creates is stamped `execution_mode:"simulated"` and `simulated:true`. It is
  NEVER presented as real model inference. The UI/SDK must surface that stamp.
- A FAILED/CANCELLED/TIMED_OUT job is never recorded as COMPLETED and (Phase 5)
  never billed as success. Phase 4 does not charge.
- No fabricated metrics. Duration/resource numbers come from the executor.

## Finite-state machine (enforce transitions transactionally; append-only job_events)
```
CREATED -> VALIDATING -> QUEUED -> MATCHED -> RESERVED -> RUNNING -> VERIFYING -> COMPLETED
```
Side/terminal states & allowed transitions:
- VALIDATING -> FAILED            (invalid spec/model/budget)
- QUEUED -> FAILED                (no eligible provider AND retries exhausted)
- QUEUED -> MATCHED -> RESERVED   (scheduler picks machine, atomically reserves)
- RESERVED -> RUNNING             (agent claims)
- RUNNING -> VERIFYING -> COMPLETED
- RUNNING -> FAILED               (executor error / agent reports failure)
- RUNNING -> TIMED_OUT            (max_runtime exceeded OR lease expired, no retries left)
- RUNNING -> RETRY_PENDING -> QUEUED   (lease expired / provider disconnect, retries left)
- CREATED/VALIDATING/QUEUED/MATCHED/RESERVED/RUNNING -> CANCELLED  (customer cancel, honored at next safe point)
- COMPLETED/FAILED/VERIFYING -> DISPUTED (Phase 5)
Every transition writes an immutable `job_events` row: {job_id, seq, from_state,
to_state, reason, actor (customer|scheduler|agent|system), created_at}. Illegal
transitions are rejected (409) and never mutate state.

## DB tables (portable types; Numeric money; JSON for specs)
`models`, `model_versions` (seed one approved simulated model `echo-sim-v1`,
workloads [text-generation,batch-inference], runtime "simulated", trust_status
"approved"), `jobs`, `job_events`, `job_assignments`, `job_attempts`,
`job_logs_metadata`, `artifacts`. Reuse `resource_reservations` from Phase 3.

`jobs` columns (at least): id, customer_id, project_id?, workload_type, model,
input JSON, status, machine_id?, reservation_id?, gpu_count, estimated_minutes,
max_cost Numeric, max_runtime_seconds, retry_policy JSON (max_retries),
attempts_made int, priority int, execution_mode, idempotency_key? (unique per
customer), lease_expires_at?, verification JSON?, final_cost Numeric?,
error_code?, error_message? (sanitized), created_at, updated_at, started_at?,
finished_at?.

## Customer endpoints (JWT)
### POST /v1/jobs   (optional header `Idempotency-Key`)
```
req: {
  "workload_type": "text-generation",
  "model": "echo-sim-v1",
  "input": { "prompt": "Hello", "max_tokens": 64 },      // batch: {"items":[...]}
  "machine_id": "uuid"?,          // optional target; omit to let the scheduler pick
  "gpu_count": 1, "estimated_minutes": 5,
  "max_cost": "5.00",             // budget cap; validated against the quote
  "max_runtime_seconds": 300,
  "retry_policy": { "max_retries": 1 }?,
  "required_gpu_runtime": "cuda" | "rocm" | "any",  // default "cuda" (D3)
  "isolation_tier": "container" | "gvisor" | "microvm" | "sealed" | "sealed_pro" | "confidential"
}
res 201: <job object>   # status starts at "created"; pipeline advances it
```
Idempotent: same (customer, Idempotency-Key) returns the SAME job (no duplicate).
Validation (VALIDATING): model approved + supports workload (else 422
model_not_supported); input schema valid for workload (422 invalid_input);
gpu_count <= target/any machine gpus; estimated cost <= max_cost (409
budget_exceeded); D5 tier rule: a ROCm GPU job with a non-container tier is rejected with HTTP 409 `rocm_tier_<tier>_unsupported`.
On success -> QUEUED.

Scheduling (QUEUED->MATCHED->RESERVED): the scheduler applies HARD eligibility
via the canonical `is_gpu_compatible` helper (runtime match, D5 AMD container-only
tier, image exists in `model_version_images` for the machine's runtime) then a
transparent WEIGHTED score (price, success_rate-if-any, availability) and picks
the best machine, then atomically reserves capacity (Phase 3 invariant — no
double-book). If none eligible: stays QUEUED (retried) with logged skip reason,
and when retries exhausted, -> FAILED (no_provider). The match is explainable:
`job.match_explanation` lists the chosen machine + score factors.

### GET /v1/jobs / GET /v1/jobs/{id}   (own only; 404 otherwise)
Returns the job incl. status, execution_mode, required_gpu_runtime, match_explanation,
verification, final_cost (null in Phase 4), timestamps.
### GET /v1/jobs/{id}/events           -> ordered FSM history (job_events)
### GET /v1/jobs/{id}/logs             -> log lines (from object storage via metadata)
### GET /v1/jobs/{id}/artifacts        -> [{artifact_id, kind, size, sha256, content_type, created_at}]
### GET /v1/jobs/{id}/artifacts/{artifact_id}/download -> short-lived signed download URL (owner only)
### POST /v1/jobs/{id}/cancel          -> requests cancellation (-> CANCELLED at next safe point); ack returned

## Agent endpoints (machine HMAC auth — same signing scheme as Phase 2 heartbeat:
X-ComputeCoin-Machine/Timestamp/Signature over canonical `machine_id\n ts \n sha256(body)`, key = signing_key)
### POST /v1/agents/jobs/claim
Body `{ "machine_id": "<id>" }`. Returns the next job in RESERVED for this machine
and transitions it RESERVED->RUNNING, sets lease_expires_at = now+LEASE_TTL, and
returns a job spec + a short-lived signed artifact upload grant. The job spec's
`image_digest` is resolved per the claiming machine's runtime via `image_for_runtime(mv, machine.gpu_runtime)`:
```
res 200: {
  "job_id": "uuid", "workload_type": "...", "model": "echo-sim-v1",
  "input": {...}, "execution_mode": "simulated",
  "limits": { "max_runtime_seconds": 300, "memory_mb": 2048, "cpus": "1.0", "gpu_count": 1 },
  "image_digest": null,                 // null => simulated; a pinned digest for the machine's runtime => DockerExecutor
  "artifact_upload": { "url": "...", "token": "...", "expires_at": "..." },
  "lease_expires_at": "..."
}
res 204: no job available.
```
A job is claimable ONCE (claiming is atomic; a second claim gets 204/409).
### POST /v1/agents/jobs/{id}/heartbeat   -> extend lease, report progress/metrics (RUNNING). `{ "progress": 0.5, "metrics": {...} }`
### POST /v1/agents/jobs/{id}/logs        -> append a log chunk `{ "lines": ["..."] }` (stored to object storage; metadata row)
### POST /v1/agents/jobs/{id}/result      -> submit the receipt (idempotent):
```
req: {
  "status": "success" | "failed",
  "execution_mode": "simulated",
  "output": { ... }?,                   // inline small outputs; large -> artifact
  "artifacts": [ { "artifact_id": "uuid", "kind": "output", "sha256": "...", "size": 123, "content_type": "application/json" } ],
  "metrics": { "duration_seconds": 1.23, "max_memory_mb": 50, "gpu_util_pct": null },
  "evidence": { "output_schema_valid": true },
  "error": { "code": "...", "message": "..." }?
}
res 200: { "job_id": "...", "status": "completed" | "failed" }
```
Server verifies (VERIFYING): output schema per workload (text-generation ->
{text, ...}; batch -> manifest/item count/checksums), records verification, then
-> COMPLETED (status success) or -> FAILED (status failed). Releases the
reservation. Container exit 0 alone is NOT treated as proof of useful work.

## Artifact upload (dev local-FS store behind a signed grant; prod S3/MinIO)
Agent uploads bytes with a PUT to `artifact_upload.url` carrying the grant token
(HMAC, short-lived, scoped to job_id). Dev store writes under a data dir and is
served back via the signed download endpoint. Limits: max size, content-type
allowlist, sha256 recorded. The API process NEVER executes customer code; it only
stores/serves bytes.

## Leases, timeout, recovery (acceptance: a disconnected provider never strands a job)
- RUNNING jobs carry lease_expires_at. Agent heartbeats extend it.
- A lease sweeper (and a lazy check on read) detects lease_expires_at < now:
  increment attempts_made; if attempts_made <= max_retries -> RETRY_PENDING ->
  QUEUED (release the reservation, re-schedule); else -> TIMED_OUT.
- max_runtime_seconds exceeded while RUNNING -> TIMED_OUT (retries honored first if
  configured). Cancellation requested -> CANCELLED at the next safe checkpoint.
- All of this writes job_events; the reservation is always released on a terminal
  or requeue transition so capacity never leaks.

## Isolation (provider agent) — hard rules, documented in code + docs
Customer workloads run ONLY via an approved Executor. The agent MUST NOT: exec
arbitrary customer shell; mount the host filesystem, the Docker socket, or host
credentials; grant unrestricted devices. DockerExecutor runs: pinned image by
digest, `--network none` (unless the workload profile explicitly needs egress,
then an allowlist), `--read-only` rootfs + a dedicated writable tmpfs/output dir,
`--cap-drop ALL`, non-root user, `--pids-limit`, `--memory`, `--cpus`, `--gpus`
when GPU, and a hard timeout. Ordinary containers are NOT claimed to guarantee
complete isolation (see ADR-0003). SimulatedExecutor runs in-process
deterministic transforms with NO network and NO filesystem escape, clearly marked
simulated.

## Config
LEASE_TTL_SECONDS=60, JOB_MAX_RUNTIME_DEFAULT=300, JOB_POLL_INTERVAL_SECONDS=5,
SCHEDULER_WEIGHTS (price/reliability/availability), ARTIFACT_MAX_BYTES,
ARTIFACT_CONTENT_TYPES. Object store: OBJECT_STORAGE_* (Phase 0) + a local-FS
fallback dir for dev.

## Isolation of customers (data)
A customer can only see/cancel their own jobs/logs/artifacts (others -> 404). An
agent can only claim/affect jobs reserved to ITS machine. Logs/outputs are private
by default.
