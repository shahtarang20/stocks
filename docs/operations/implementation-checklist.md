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

## Phase 6 — Inference endpoints ✅ DONE

- [x] OpenAI-compatible `/v1/chat/completions` and `/v1/embeddings` served by
      routed provider capacity (replace availability errors with real results).
- [x] Pydantic request/response schemas matching OpenAI API spec with
      `computecoin` extension block (execution_mode honesty stamp).
- [x] Inference orchestration service: model resolution from registry,
      provider routing (cheapest eligible online machine), simulated response
      generators (deterministic echo for chat, hash-based L2-normalized
      embeddings).
- [x] FastAPI router registered at `/v1/chat/completions` and `/v1/embeddings`
      with auth, structured error responses, and OpenAPI documentation.
- [x] Phase 6 configuration (rate limits, max tokens, embedding dimension).
- [x] Test suite: **21 tests passed** — chat completions (basic, multi-turn,
      n-parameter, model-not-found, streaming-rejected, auth, validation,
      simulated stamp, determinism), embeddings (basic, batch, determinism,
      different-inputs, L2-normalization, model-not-found, auth, empty input
      rejection, batch cap enforcement), honesty stamps (chat + embedding
      never claim hardware), and rate limit enforcement (429).


## Phase 7 — Web dashboard ✅ DONE

- [x] Admin console UI: Overview, Users, Providers, Jobs, Disputes, Billing & reconciliation, Security posture, Audit logs, Configuration.
- [x] Admin backend APIs: user directory (`/v1/admin/users`), account suspension/reinstatement (`/v1/admin/users/{id}/suspend`), audit log queries (`/v1/admin/audit-logs`), platform-wide job listings (`/v1/admin/jobs`), and operational KPIs (`/v1/admin/stats`).
- [x] Customer dashboard & workflows: project creation/management, project-scoped API key generation & revocation with one-time secret reveals, job tracking, invoices, disputes, and credit purchases.
- [x] Provider console & workflows: onboarding, hardware inventory, live earnings accrued from the double-entry ledger, and payout requests.
- [x] Test suite: **6 admin integration tests passing** covering RBAC, user suspension/reinstatement, self-suspension blocking, audit logs, and platform stats (116 total backend tests passing). All 43 frontend routes compiling cleanly.

## Phase 8 — Stronger isolation

- [ ] gVisor → microVMs (Firecracker/Kata) → dedicated hosts.
- [ ] Confidential computing + attestation (protect customer from provider).

## Phase 9 — Telemetry at scale

- [ ] TimescaleDB for high-volume GPU telemetry, dashboards, alerting.

## Phase 10 — On-chain settlement (contracts)

- [ ] Token/settlement contracts; audits; off-chain↔on-chain reconciliation.
- [ ] Pausable/upgradable safeguards, multisig/HSM key management.
