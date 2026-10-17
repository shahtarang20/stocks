# ComputeCoin — Implementation Checklist (10-phase plan)

Tracks the build phase by phase. Phase 0 is done; Phase 1 is in progress.
`[x]` done · `[~]` in progress / skeleton · `[ ]` not started.

## Phase 0 — Foundations & decisions ✅ DONE

- [x] ADR-0001: PostgreSQL as transactional source of truth; Redis cache/leases;
      S3-compatible artifacts; MongoDB rejected for the money core; Supabase as
      hosted Postgres+Storage; Timescale noted for future telemetry.
- [x] ADR-0002: Argon2id, short access JWT + rotating opaque refresh tokens with
      reuse detection, server-side RBAC, hashed API keys (prefix shown), audit
      logs; why not Supabase Auth.
- [x] ADR-0003: container isolation baseline; no docker socket / host mounts;
      microVMs/dedicated hosts as the strong path; explicit "containers are not
      complete isolation" statement.
- [x] Architecture overview + job FSM (CREATED→…→COMPLETED).
- [x] Threat model (STRIDE) for API, scheduler, agent, artifacts, ledger,
      future contracts, with residual risk.
- [x] Repo scaffolding for `provider-agent/`, `sdk/`, `docs/`.

## Phase 1 — Agent, SDK & contracts skeleton 🔨 IN PROGRESS

- [x] provider-agent package `computecoin_agent` (Python 3.12).
- [x] Hardware detection (CPU/RAM/disk/GPU), CPU-safe, mocked GPU; typed
      CapabilityReport; DETECTED vs VERIFIED.
- [x] Enrollment with revocable machine-scoped token; machine keypair/HMAC;
      0600 config.
- [x] Signed heartbeats with injectable transport.
- [x] CLI: `detect`, `enroll`, `run`, `security-info`.
- [x] provider-agent tests green (CPU-only, no-VERIFIED, signed heartbeat,
      0600 config). **17 passed.**
- [x] Python SDK `computecoin` (register/login/refresh/projects/api-keys/
      chat_completions/embeddings), 404/501 → EndpointNotAvailableError. **6 passed.**
- [x] TypeScript SDK `@computecoin/sdk` mirroring Python; `tsc --noEmit` passes;
      vitest **3 passed**.
- [x] READMEs for agent + both SDKs.
- [ ] Backend FastAPI: auth endpoints (register/login/refresh) wired to Postgres.
- [ ] Backend: projects + API keys persisted.
- [ ] Agent↔API enroll exchange (server-issued machine_id) and heartbeat ingest.
- [ ] CI running all three test suites.

## Phase 2 — Core API & billing ledger

- [ ] Postgres schema + migrations (users, projects, api_keys, machines,
      ledger, reservations) with FKs/constraints.
- [ ] Double-entry ledger with exact NUMERIC; reserve/capture/release in one txn.
- [ ] RBAC enforcement + audit log.
- [ ] Rate limiting + quotas (Redis).

## Phase 3 — Scheduler & leases

- [ ] Job submission + validation; funds reservation on QUEUED.
- [ ] Matching engine; Redis TTL leases; lease expiry → release funds.
- [ ] Job FSM enforcement + transition audit.

## Phase 4 — Job execution & isolation (agent)

- [ ] Hardened container runtime (non-root, no docker socket, no host mounts,
      dropped caps, seccomp, read-only rootfs, restricted egress, quotas).
- [ ] Artifact upload/download (signed URLs, checksums).
- [ ] Heartbeat-driven job status; timeouts → EXPIRED.

## Phase 5 — Verification

- [ ] Benchmark/attest GPU capabilities; promote DETECTED→BENCHMARKED/VERIFIED.
- [ ] Periodic re-verification + result spot-checks; reputation signals.

## Phase 6 — Inference endpoints

- [ ] OpenAI-compatible `/v1/chat/completions` and `/v1/embeddings` served by
      routed provider capacity (replace availability errors with real results).

## Phase 7 — Web dashboard

- [ ] Customer + provider UIs (projects, keys, jobs, billing, enrollment).

## Phase 8 — Stronger isolation

- [ ] gVisor → microVMs (Firecracker/Kata) → dedicated hosts.
- [ ] Confidential computing + attestation (protect customer from provider).

## Phase 9 — Telemetry at scale

- [ ] TimescaleDB for high-volume GPU telemetry, dashboards, alerting.

## Phase 10 — On-chain settlement (contracts)

- [ ] Token/settlement contracts; audits; off-chain↔on-chain reconciliation.
- [ ] Pausable/upgradable safeguards, multisig/HSM key management.
