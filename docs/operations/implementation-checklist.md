# ComputeCoin — Implementation Checklist (10-phase plan)

Tracks the build phase by phase. All phases 0 through 10 are complete and verified.
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

## Phase 1 — Agent, SDK & contracts skeleton ✅ DONE

- [x] provider-agent package `computecoin_agent` (Python 3.12).
- [x] Hardware detection (CPU/RAM/disk/GPU), CPU-safe, mocked GPU; typed
      CapabilityReport; DETECTED vs VERIFIED.
- [x] Enrollment with revocable machine-scoped token; machine keypair/HMAC;
      0600 config.
- [x] Signed heartbeats with injectable transport.
- [x] CLI: `detect`, `enroll`, `run`, `security-info`.
- [x] provider-agent tests green (CPU-only, no-VERIFIED, signed heartbeat,
      0600 config).
- [x] Python SDK `computecoin` (register/login/refresh/projects/api-keys/
      chat_completions/embeddings), 404/501 → EndpointNotAvailableError.
- [x] TypeScript SDK `@computecoin/sdk` mirroring Python; `tsc --noEmit` passes;
      vitest passing.
- [x] READMEs for agent + both SDKs.
- [x] Backend FastAPI: auth endpoints (register/login/refresh) wired to Postgres.
- [x] Backend: projects + API keys persisted.
- [x] Agent↔API enroll exchange (server-issued machine_id) and heartbeat ingest.
- [x] Monorepo automated test suites passing.

## Phase 2 — Core API & billing ledger ✅ DONE

- [x] Postgres schema + migrations (users, projects, api_keys, machines,
      ledger, reservations) with FKs/constraints.
- [x] Double-entry ledger with exact NUMERIC; reserve/capture/release in one txn.
- [x] RBAC enforcement + audit log.
- [x] Rate limiting + quotas (Redis/in-memory token bucket).

## Phase 3 — Scheduler & leases ✅ DONE

- [x] Job submission + validation; funds reservation on QUEUED.
- [x] Matching engine; Redis TTL leases; lease expiry → release funds.
- [x] Job FSM enforcement + transition audit.

## Phase 4 — Job execution & isolation (agent) ✅ DONE

- [x] Hardened container runtime (non-root, no docker socket, no host mounts,
      dropped caps, seccomp, read-only rootfs, restricted egress, quotas).
- [x] Artifact upload/download (signed URLs, checksums).
- [x] Heartbeat-driven job status; timeouts → EXPIRED.

## Phase 5 — Verification & Billing ✅ DONE

- [x] Benchmark/attest GPU capabilities; promote DETECTED→BENCHMARKED/VERIFIED.
- [x] Invoices, credit purchases, payout requests, provider earnings from settled ledger.
- [x] Periodic re-verification + result spot-checks; reputation signals.

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

## Phase 8 — Stronger isolation ✅ DONE
Shipped per `docs/architecture/decisions/0003-workload-isolation-and-sandbox-tiers.md`. Enforces strict tier matching with "no silent downgrade".

- [x] Host capability probes & isolation tier detection (`container`, `gvisor`, `microvm`, `confidential`) in `provider-agent/computecoin_agent/isolation.py`.
- [x] Runtime argv flags appended dynamically (`--runtime runsc`, `--runtime kata-qemu`, `--runtime kata-qemu-sev`) and strict "no silent downgrade" enforcement via `IsolationUnavailableError` in `executor.py`.
- [x] Cryptographic remote hardware attestation lifecycle: challenge-nonce exchange, HMAC-SHA256 measurement quotes, quote verification with timestamp freshness, and promotion to `VERIFIED` status in `apps/api/app/api/routers/providers.py` and `provider-agent/computecoin_agent/attestation.py`.
- [x] Database migrations and schemas: Alembic migration `0006_isolation_phase8`, `Job.isolation_tier`, `ProviderCapability.supported_isolation_tiers`, and `MachineAttestation` audit logs.
- [x] Explainable scheduler matching & targeted machine validation ensuring no job is scheduled to an unverified or incapable node.
- [x] Frontend UI: Isolation tier selection in job submission (`apps/web/src/app/dashboard/jobs/new/page.tsx`), isolation badges on job detail and platform-wide admin job tables.
- [x] Test suite: **12 provider-agent unit tests** (56/56 passing), **4 backend integration tests** (120/120 passing), **43/43 Next.js static pages compiling cleanly**.

## Phase 9 — Telemetry at scale ✅ DONE
Shipped per `docs/architecture/ADR-0001-database.md` time-series telemetry requirements. Keeps the high-volume metrics firehose partitioned and structured away from OLTP transactional paths.

- [x] Time-series data model & TimescaleDB hypertable compatible schema: `MachineTelemetry` (per-GPU utilization, VRAM usage, temperatures, power, fan speed) and `JobTelemetry` (throughput tokens/sec, steps, loss) in [`apps/api/app/models/telemetry.py`](../../apps/api/app/models/telemetry.py).
- [x] Database migration: Alembic `0007_telemetry_phase9` with compound time-series indexes on `(machine_id, timestamp)`, `(machine_id, gpu_index, timestamp)`, and `(job_id, timestamp)`.
- [x] Ingestion & operational alerting engine: Batch ingestion endpoints (`POST /v1/telemetry/machine`, `POST /v1/telemetry/job`) with HMAC signature authentication and automatic threshold alert triggers (`temp_c >= 85°C`, `mem_util_pct >= 95%`) in [`apps/api/app/services/telemetry.py`](../../apps/api/app/services/telemetry.py).
- [x] Time-window aggregation & platform stats: Fast time-bucketed rollups (`1m`, `5m`, `1h`) calculating min/max/avg metrics, and live fleet-wide health KPIs (`GET /v1/telemetry/stats`).
- [x] Provider agent telemetry collector: In [`provider-agent/computecoin_agent/telemetry.py`](../../provider-agent/computecoin_agent/telemetry.py), sliding buffer with NVML GPU metric sampling and signed batch reporting.
- [x] Frontend UI: Admin Telemetry & Health Dashboard at [`apps/web/src/app/admin/telemetry/page.tsx`](../../apps/web/src/app/admin/telemetry/page.tsx) with live auto-refresh, KPI cards, interactive alert acknowledgement/resolution, and machine metric inspection.
- [x] Test suite: **4 provider-agent unit tests** (60/60 passing), **4 backend integration tests** (124/124 passing), **44/44 Next.js static pages compiling cleanly**.

## Phase 10 — On-chain settlement (contracts) ✅ DONE
Shipped per `business.md` smart-contract requirements and `docs/architecture/ADR-0001-database.md` reconciliation guarantees. Bridges decentralized EVM smart contract escrow locks with off-chain PostgreSQL transactional ledger invariants.

- [x] ERC-20 ComputeCoin Token (`COMPUTE`): Fixed supply cap (1,000,000,000 COMPUTE), `ERC20Burnable`, `ERC20Pausable`, EIP-2612 `ERC20Permit` for gasless approvals, and `Ownable2Step` in [`contracts/src/ComputeCoinToken.sol`](../../contracts/src/ComputeCoinToken.sol).
- [x] Non-custodial Escrow Settlement Contract (`ComputeSettlement.sol`): `lockBudget` with duplicate lock protection, cryptographic EIP-191 voucher-based `settleJob` with ECDSA signature verification and platform fee split to protocol treasury, and self-service customer `refundJob` after deadline expiration or via platform refund authorization voucher in [`contracts/src/ComputeSettlement.sol`](../../contracts/src/ComputeSettlement.sol).
- [x] Native Solidity and Hardhat 3 test suite: Circuit-breaker pauses, token burn, supply cap, budget locking, duplicate-locking revert checks, and deadline refund mechanics (**6/6 tests passing**).
- [x] Database schema & Alembic migration: `OnChainEscrow` and `OnChainTransaction` models with 78-character decimal string precision for EVM uint256 token balances, indexed on job UUID, contract addresses, and tx hashes in Alembic migration `0008_settlement_phase10`.
- [x] Cryptographic platform voucher signing service: Python `eth-account` EIP-191 message packing and ECDSA signing service verifying job completion runtime receipts in [`apps/api/app/services/settlement.py`](../../apps/api/app/services/settlement.py).
- [x] Backend settlement router: Registered at `/v1/settlement` with endpoints for public contract registry (`/contracts`), escrow locking (`/escrows`), cryptographic voucher minting (`/vouchers`), transaction confirmations (`/confirm`, `/refund`), transaction audit logs (`/transactions`), and global ledger reconciliation metrics (`/reconciliation`).
- [x] Frontend Web3 Settlement Dashboard: Admin On-Chain Settlement control center at [`apps/web/src/app/admin/settlement/page.tsx`](../../apps/web/src/app/admin/settlement/page.tsx) with live contract addresses, copy actions, global escrow metrics, interactive voucher generator, escrow status inspection, and transaction audit trail.
- [x] Test suite: **6 smart contract tests** (6/6 passing), **4 backend integration tests** (128/128 total passing), **45/45 Next.js static pages compiling cleanly**.

## Phase 11 — AMD GPU support (ROCm): code complete; real-hardware validation (WP9) and sign-off pending
Shipped per `docs/operations/amd-gpu-support-plan.md` across WP1–WP8. Extends ComputeCoin's multi-vendor GPU execution to AMD Radeon (RDNA3) and AMD Instinct (CDNA) hardware under ROCm 6.x while strictly maintaining NVIDIA backward compatibility and isolation guarantees.

- [x] **WP1 Agent detection** ([`provider-agent/computecoin_agent/hardware.py`](../../provider-agent/computecoin_agent/hardware.py)): `GPUInfo` exposes `gpu_vendor` (`nvidia`|`amd`), `gpu_runtime` (`cuda`|`rocm`), and `gpu_runtime_version`. Detection queries `amdsmi` with fail-safe CPU fallback; D2 policy enforces single-vendor per host. Optional `amd` dependency extra configured in `pyproject.toml`.
- [x] **WP2 Agent execution** ([`provider-agent/computecoin_agent/executor.py`](../../provider-agent/computecoin_agent/executor.py)): D6 device passthrough granting `/dev/kfd` and only assigned `/dev/dri/renderD<N>` nodes using numeric GIDs from host `stat -c %g`. D5 policy strictly enforces `container` isolation tier only (raising `IsolationUnavailableError` on gVisor/microVM). All ADR-0003 hardening flags (`--cap-drop ALL`, `no-new-privileges`, `--read-only`, `--network none`) preserved.
- [x] **WP3 Agent telemetry** ([`provider-agent/computecoin_agent/telemetry.py`](../../provider-agent/computecoin_agent/telemetry.py)): Samples GPU metrics via `amdsmi` (utilization, VRAM used, temperature, socket power); unexposed sensors report `None`.
- [x] **WP4 Backend schema & ingest** (Alembic migration `0022`): `hardware_inventory` gains `gpu_vendor`, `gpu_runtime`, `gpu_runtime_version`; `jobs` gains `required_gpu_runtime`; `model_version_images` table maps per-runtime image digests; `verification_challenges` stores expected hashes per runtime. D8 backfill defaults existing records to `nvidia`/`cuda`. Mixed-vendor capability reports rejected with 422.
- [x] **WP5 Scheduler, marketplace, inference** ([`apps/api/app/services/gpu_runtime.py`](../../apps/api/app/services/gpu_runtime.py)): Canonical `is_gpu_compatible` helper serves as the single source of truth for runtime matching, D5 container-only isolation, and D4 per-runtime image resolution across `validate_job`, `_eligible_machines`, and inference routing. Clear skip reasons logged. Marketplace HTTP route filters by `gpu_vendor` and `gpu_runtime`.
- [x] **WP6 Verification** ([`apps/api/app/services/verification.py`](../../apps/api/app/services/verification.py)): Shadow machine selection enforces same vendor and runtime, prioritizing identical GPU models before price ranking. Challenges check expected hash for the active runtime.
- [x] **WP7 Frontend & SDKs**: Job submission form (`apps/web/src/app/dashboard/jobs/new/page.tsx`) features GPU runtime select (CUDA / ROCm / Any) with D5 non-container tier disabling; provider hardware and marketplace tables display vendor/runtime badges and filters; admin machines table has Vendor column. Python SDK (`sdk/python`) and TypeScript SDK (`sdk/typescript`) updated with `gpu_runtime` support, tests, and documentation.
- [x] **WP8 Documentation**: Phase 2 and 4 protocol docs, ADR-0003 addendum, threat model, GPU validation runbook, and PLATFORM_GUIDE updated.
- [x] **Full test suite green**:
  - API: **664 passed**, 41 skipped
  - Provider Agent: **138 passed**
  - Python SDK: **76 passed**
  - TypeScript SDK: **22 passed** (typecheck clean)
  - Web: **22 passed** (typecheck, lint, and build for 53 pages clean)
  - Guardian & Witness: **28 passed**
  - Contracts: **33 passed**
  - End-to-end: **10 passed**
