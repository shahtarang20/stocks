# ComputeCoin — Project Review & Upgrade Roadmap

_Review date: 2026-10-22 · Branch: `main` · Scope: whole repository (API, web, provider agent, SDKs, contracts, infra, docs)_

> **How this review was done.** It is based on reading the README, `AUDIT_REPORT.md`, the implementation checklist, the API layout, and targeted code inspection (scheduler scoring, inference service, rate limiter, web token handling, Sentry wiring, agent transport). The test suites were **not** re-run as part of this review. Statements are tagged **[verified]** when confirmed in code, and **[to confirm]** when inferred and worth a quick check before acting.

---

## 1. Executive summary

ComputeCoin is a decentralized GPU compute marketplace: FastAPI + Postgres (double-entry ledger), Next.js dashboards (customer / provider / admin), an installable provider agent, Python and TypeScript SDKs, and Solidity settlement contracts.

**The engineering foundations are unusually mature for the stage** — a transactional ledger, strong auth, hardware attestation, isolation tiers, honest labelling of simulated output, and a long list of audited-and-fixed security issues.

**The central gap is that the core product is not real yet.** Inference responses are deterministic simulated text and hash-based embeddings (`apps/api/app/services/inference.py`). Nothing a customer pays for is currently produced by a GPU. Everything else — scheduler, billing, verification, settlement — is built around a workload that is still a stub.

### Top five priorities

| # | Priority | Why it matters |
|---|---|---|
| 1 | Real inference execution on provider GPUs (with streaming) | Without it there is no product to sell |
| 2 | Verification of provider *results*, not just hardware | A marketplace without result integrity can be cheated |
| 3 | Finish the auth hardening (remove leftover `localStorage` token mirror) and the contract jobId fix | Last open security items before public exposure |
| 4 | Observability and operational tooling | Required to run a money-handling platform safely |
| 5 | Make the scheduler learn from real history (reliability is currently a constant) | Quality of routing is the marketplace's main differentiator |

---

## 2. What is already strong (keep and protect)

- **Money correctness.** Postgres as source of truth, exact `NUMERIC` amounts, double-entry ledger, reserve/capture/release inside one transaction, cumulative refund caps, no-negative floors on provider and platform accounts, per-customer job-submit serialization to avoid deadlocks (commit `8e01747`).
- **Authentication and authorization.** Argon2id, short-lived access JWTs, rotating refresh tokens with reuse detection, hashed API keys, server-side RBAC, audit logs, email verification enforced on spend/compute (commit `aa9985c`), password reset, login-security tests, CSRF tests.
- **Boot-time safety.** The API refuses to start in non-dev environments with dev default secrets (`core/config.py` validator, tested).
- **Provider trust model.** Signed heartbeats, machine-scoped revocable tokens, challenge/response attestation, DETECTED → BENCHMARKED → VERIFIED promotion, isolation tiers (`container`, `gvisor`, `microvm`, `confidential`) with an explicit **no silent downgrade** rule.
- **Honesty discipline.** Every inference response carries an `execution_mode` stamp (`simulated` | `hardware`); docs state what is and is not built. Keep this — it is a trust asset.
- **Test and delivery baseline.** ~141 API tests, plus agent, SDK and contract tests; CI and deploy workflows exist (`.github/workflows/ci.yml`, `deploy.yml`); Sentry is wired in (`SENTRY_DSN`).
- **Documentation.** ADRs for database, auth and isolation; a threat model; an audit report with proving tests.

---

## 3. Findings and recommended upgrades

Each item lists **current state → problem → recommendation → effort/impact**.

### 3.1 Real inference execution (CRITICAL — product gap)

- **Current state [verified].** `inference.py` generates `"[ComputeCoin Simulated] …"` chat output and hash-based embeddings. `ChatCompletionRequest.stream` exists, but streaming is rejected.
- **Problem.** No real model ever runs. Token counts are estimated, not measured. Streaming is the default mode for most OpenAI-compatible clients (SDKs, chat UIs, agent frameworks), so rejecting it makes the endpoint unusable in practice.
- **Recommendation.**
  1. Add a **model runtime layer** in the provider agent that launches an inference server (vLLM first; llama.cpp for CPU/low-VRAM; TGI optional) inside the existing hardened container profile.
  2. Define an **agent ↔ API inference contract**: request envelope, token-level streaming back through the API, cancellation, and timeouts.
  3. Implement **SSE streaming** on `/v1/chat/completions` (`stream=true`), with a correct final `usage` chunk and `[DONE]` sentinel.
  4. **Meter real usage**: bill prompt/completion tokens as reported by the runtime and cross-checked by the API, not `_estimate_tokens`.
  5. Add **function/tool calling** and **JSON mode** passthrough where the runtime supports it.
  6. Ship a **curated model catalog** (e.g. a small Llama/Qwen/Mistral set plus one embedding model) with VRAM requirements recorded in the model registry so the scheduler can match on them.
  7. Keep `execution_mode="simulated"` for CI and demos; real runs must say `hardware`.
- **Acceptance test.** One real model, on one real GPU, answering a streamed request through the public API, with the ledger charging the measured token count.
- **Effort / impact.** Large / **highest**.

### 3.2 Verification of provider results (HIGH)

- **Current state.** Attestation proves *hardware claims*. Checklist item "result spot-checks" exists, but with simulated output there is nothing real to check.
- **Problem.** A malicious or faulty provider could return garbage, a cheaper model, or truncated output while still being paid.
- **Recommendation.**
  - **Known-answer challenge jobs** injected at random: deterministic prompt (temperature 0, fixed seed) whose expected output is known; mismatch lowers reputation.
  - **Redundant execution** on a configurable sample (e.g. 1–5 %) of paid jobs on a second provider; compare outputs (exact for greedy decoding, embedding-distance / logprob tolerance otherwise).
  - **Model-identity checks** (weight hash reported by the agent and validated against the registry).
  - **Reputation and slashing**: feed outcomes into a provider reliability score, withhold or claw back earnings on proven fraud, and expose the history in the provider console.
  - A **dispute workflow** that attaches evidence (inputs, outputs, hashes, challenge results) so admins can resolve cases consistently.
- **Effort / impact.** Large / very high.

### 3.3 Scheduler quality (HIGH)

- **Current state [verified].** `services/scheduler.py::_score_candidates` weights price, reliability and availability. **Reliability is hard-coded to `0.5`** for every machine ("no completed-job history yet"), so it contributes nothing to ranking.
- **Problem.** Routing is effectively cheapest-with-free-slots. There is no signal for latency, locality, cache warmth, or past failures.
- **Recommendation.**
  - Compute **reliability from real history**: completion rate, timeouts, lease expiries, verification failures, uptime from heartbeats — with a time-decayed window and a sensible prior for new machines.
  - Add scoring factors: **region/latency**, **warm model cache** (avoid reloading weights), **queue depth**, **GPU class fit** (don't waste an 80 GB card on a 7B model unless cheaper), **isolation tier**.
  - **Retry/failover**: when a machine drops mid-job, reschedule on another with idempotent billing.
  - **Pricing modes**: on-demand vs interruptible (spot) vs reserved; dynamic pricing from supply/demand.
  - Keep the existing **explainable factors** payload — it is good practice; extend it with the new factors.
- **Effort / impact.** Medium / high.

### 3.4 Authentication: finish the cookie migration (HIGH, security)

- **Current state [verified].** The backend sets httpOnly cookies and CSRF handling exists (`routers/auth.py`, `test_cookie_auth_csrf.py`), and `auth-context.tsx` documents that tokens are not kept in JS-accessible storage. **However `apps/web/src/lib/api.ts` (≈ lines 859–925) still reads/writes `computecoin_access_token`, `computecoin_refresh_token` and `computecoin_csrf_token` via `localStorage`** — apparently as a mirror or fallback.
- **Problem.** Any XSS can read those values, which defeats the point of httpOnly cookies. `AUDIT_REPORT.md` still lists this as an open risk.
- **Recommendation.**
  1. Remove access and refresh tokens from `localStorage` entirely; keep only in-memory state if needed. Rely on the httpOnly cookie for the session.
  2. The CSRF token may stay readable (double-submit pattern) but should live in a non-httpOnly cookie or in memory, not `localStorage`.
  3. Keep the bearer-token path **only** for SDK/API-key clients.
  4. Add a Playwright/Jest test asserting `localStorage` contains no credentials after login.
  5. Update `AUDIT_REPORT.md` to close the risk.
- **Effort / impact.** Small / high.

### 3.5 Smart contracts and on-chain settlement (MEDIUM now, CRITICAL before mainnet)

- **Open items (from `AUDIT_REPORT.md`).**
  - **jobId squatting:** anyone can `lockBudget` for a known jobId. Fix by keying escrows on `(customer, jobId)`; needs a redeploy and a backend change.
  - **Ledger parity** is reported from an internal flag, not a real chain comparison. Add a reconciler that reads on-chain escrow state/events and compares to `OnChainEscrow` rows and the off-chain ledger.
  - **`settleJob` happy path** lacks an end-to-end test with a real signed voucher (ethers/JS under Hardhat 3).
  - **Voucher design:** include chain id, contract address, nonce and expiry in the signed payload (verify they already are) and test replay across chains/contracts.
- **Recommendation.** Fix the above, add property/fuzz tests (Foundry or Echidna) for escrow invariants (sum of locks = sum of payouts + refunds + fees), and commission an **external audit** before any mainnet deployment. Keep testnet-only until then.
- **Strategy note.** On-chain rails are a parallel track. Do not let them delay shipping real inference + fiat billing (Stripe already integrated).
- **Effort / impact.** Medium / high (gating for token launch).

### 3.6 Observability and operations (HIGH)

- **Current state.** Sentry and a `/health` endpoint exist; a telemetry schema for machines/jobs exists. **No metrics stack (Prometheus/OpenTelemetry) was found in the API [verified by search; to confirm].**
- **Recommendation.**
  - Expose **Prometheus metrics**: request rate/latency/error by route, job state counts, queue depth, lease expiries, scheduler decision latency, ledger operations, Stripe webhook outcomes, rate-limit hits.
  - **Alerts** on: stuck jobs, reserved-but-unclaimed holds, **ledger invariant drift** (sum of accounts ≠ 0), recovery-sweeper failures, provider fleet health drop, error-rate spikes.
  - **Structured logging** with request/job/machine correlation IDs; OpenTelemetry tracing across API → scheduler → agent.
  - **Runbooks** in `docs/operations/` for each alert; backup/restore and migration-rollback drills for Postgres.
  - A **nightly reconciliation job** that asserts ledger invariants and emails/pages on failure.
- **Effort / impact.** Medium / high.

### 3.7 Scalability and infrastructure (MEDIUM)

- **Rate limiter [verified].** Redis fixed window with an **in-process fallback**. With several API workers or replicas, the fallback multiplies the effective limit and resets on restart. In production, **require Redis** (fail closed or degrade to a strict low limit), and consider a sliding-window or token-bucket algorithm to avoid window-edge bursts.
- **Provider connectivity [verified: polling].** The agent polls for jobs (`--poll-interval`, default 5 s). Fine for batch; adds latency for inference and load as the fleet grows. Move to **outbound WebSocket / gRPC stream or long-poll** so providers behind NAT receive work instantly without opening inbound ports.
- **Telemetry volume.** The schema is "Timescale-compatible" but uses plain tables. Add **partitioning or a Timescale hypertable plus retention/compression policies** before data volume grows; cap ingestion per machine.
- **Background work.** The recovery sweeper runs in the app lifespan and a separate worker exists (`Procfile`). Make sure sweeper and worker are **leader-elected or lock-protected** when scaled to multiple instances (the `skip_locked` selection already helps — verify the lease sweeper too).
- **Storage.** Add lifecycle rules (artifact/log retention), virus/size limits, and signed-URL expiry review.
- **Load testing.** `scripts/staging-load-test.py` exists; run it against realistic mixes in CI-adjacent staging and record baselines (p95 submit latency, jobs/min, ledger contention).
- **Effort / impact.** Medium / medium–high.

### 3.8 Repository structure and hygiene (MEDIUM)

- **Empty service folders [verified].** `services/scheduler`, `services/job-worker`, `services/billing`, `services/verification` are empty; the logic lives under `apps/api/app/services/`. The README lists them as top-level services.
  - Either **delete the empty folders** and fix the README, or extract them deliberately with clear boundaries. Recommended for now: keep the modular monolith, delete the folders, document the decision in an ADR.
- **`apps/admin` [verified missing].** README lists `apps/admin`, but the admin UI lives in `apps/web/src/app/admin`. Update the README.
- **Local database files.** `computecoin.db` exists at the repo root and in `apps/api/`. They are git-ignored (`*.db`), so they are not committed — just clean them up locally and make sure docs say tests/dev use Postgres where row-locking matters.
- **Documentation sprawl.** Root contains `AUDIT_REPORT.md`, `business.md`, `business2.md`, `business3.md`, `qa.md`, `qa2.md`, `LAUNCH_PLAN.md`, `WHITEPAPER.md`. Consolidate under `docs/` (`docs/audit/`, `docs/business/`, `docs/qa/`), keep README and a short `CONTRIBUTING.md` at the root, and remove stale drafts.
- **Type sharing.** `packages/shared-types` exists; generate TypeScript types from the FastAPI OpenAPI schema (e.g. `openapi-typescript`) and use them in both the web app and the TS SDK to prevent drift. The web `api.ts` is large (900+ lines) — split by domain.
- **Dependency and supply-chain checks.** Add Dependabot/Renovate, `pip-audit`, `npm audit`, secret scanning, and SBOM generation to CI. Address the postcss advisory when moving to `next@16`.
- **Effort / impact.** Small / medium.

### 3.9 Testing and quality (MEDIUM)

- **Strengths.** Broad unit and integration coverage, phase-based E2E scripts, provider-agent tests.
- **Gaps to close.**
  - **Postgres-backed CI** for concurrency/locking tests (SQLite cannot validate row locks, `skip_locked`, or deadlock behavior).
  - **Property-based tests** (Hypothesis) for the ledger: random sequences of reserve/capture/release/refund must preserve invariants.
  - **Chaos tests:** provider vanishes mid-job, Redis down, DB failover, duplicate Stripe webhooks, clock skew on signed heartbeats.
  - **Frontend tests:** Playwright E2E for signup → verify email → buy credits → submit job → see result; accessibility (axe) checks.
  - **Coverage gates** and mutation testing on `ledger.py`, `billing.py`, `settlement.py`.
  - **Contract fuzzing** (see 3.5).
- **Effort / impact.** Medium / high for money paths.

---

## 4. Feature upgrades for the product

### 4.1 Customer-facing

| Feature | Detail | Priority |
|---|---|---|
| Streaming, tool calling, JSON mode | See 3.1 | High |
| Model catalog and playground | Browse models, pricing per 1M tokens, try in browser | High |
| Usage and cost analytics | Per-project, per-key, per-model charts; CSV export | High |
| Spend limits and alerts | Hard monthly cap per project/key, email/webhook at 50/80/100 % | High |
| Webhooks | Job completed/failed, low balance, invoice paid | Medium |
| Batch API | Submit many requests, async results at a discount | Medium |
| Fine-tuning and training jobs | Dataset upload, checkpointing, resume, artifact download | Medium |
| Teams / organizations | Shared projects, roles, invitations, SSO (SAML/OIDC) | Medium |
| SDK polish | Retries, streaming helpers, typed errors, published to PyPI/npm, examples | Medium |

### 4.2 Provider-facing

| Feature | Detail | Priority |
|---|---|---|
| One-line installer and auto-benchmark | Detect hardware, run benchmark, enroll, start | High |
| Earnings and uptime dashboard | Per-machine revenue, utilization, reliability score and how to improve it | High |
| Pricing controls | Floor price, schedule/availability windows, model allow-list | Medium |
| Instant job push | WebSocket/gRPC stream (see 3.7) | Medium |
| Reputation tiers | Badges and higher routing priority for reliable providers | Medium |
| Payout options | Fiat (Stripe Connect) alongside token rails; payout history and tax docs | Medium |

### 4.3 Admin / platform

| Feature | Detail | Priority |
|---|---|---|
| Fraud and abuse detection | Velocity checks on signups/top-ups, anomaly detection on usage and refunds | High |
| Ledger health page | Live invariant checks, drift alerts, drill-down to offending entries | High |
| Dispute console | Evidence view, one-click refund/penalty with audit trail | Medium |
| Feature flags and config | Change limits/weights without redeploy, with audit | Medium |
| Compliance toolkit | Data export/delete (GDPR), retention policies, KYC hooks for payouts | Medium |

### 4.4 Token / Web3 (later)

Defer incentives, staking and on-chain settlement as the primary rail until fiat usage proves demand. When resumed: fix contract items in 3.5, external audit, testnet soak, staged mainnet with caps.

---

## 5. Suggested roadmap

### Phase A — "Make it real" (weeks 1–6)
1. Remove `localStorage` token mirror; close the auth risk (3.4).
2. Agent inference runtime (vLLM) + API streaming + real token metering (3.1).
3. Model registry with VRAM requirements; scheduler matches on it.
4. Prometheus metrics, core alerts, nightly ledger reconciliation (3.6).
5. Postgres-backed CI job (3.9).
**Exit criteria:** one real model served end-to-end, streamed, billed on measured tokens, visible in dashboards.

### Phase B — "Make it trustworthy" (weeks 6–12)
1. Known-answer challenges and redundant-execution sampling (3.2).
2. Reliability scoring from real history; failover and retry (3.3).
3. Dispute workflow with evidence; provider reputation tiers.
4. Spend limits, usage analytics, webhooks.
5. Require Redis for rate limiting; WebSocket/gRPC job push (3.7).
**Exit criteria:** a cheating provider is detected and penalized in a staged test; failover completes a job after a provider crash.

### Phase C — "Make it grow" (weeks 12–20)
1. Batch API, fine-tuning/training jobs, checkpoint/resume.
2. Teams/orgs, SSO, SDK publishing.
3. Provider onboarding polish, pricing controls, Stripe Connect payouts.
4. Load-test baselines and autoscaling plan.
**Exit criteria:** external beta providers and customers onboarded without manual help.

### Phase D — "Make it on-chain" (after demand is proven)
1. Contract fixes, fuzzing, external audit (3.5).
2. Real on-chain reconciliation.
3. Testnet soak → capped mainnet.

---

## 6. Quick wins (≤ 1 day each)

- [ ] Delete `localStorage` credential storage in `apps/web/src/lib/api.ts`; add a test.
- [ ] Delete empty `services/*` folders and fix the README layout (and the `apps/admin` mention).
- [ ] Move root-level markdown files into `docs/`.
- [ ] Add Dependabot, `pip-audit`, `npm audit` and secret scanning to CI.
- [ ] Make Redis mandatory for rate limiting when `ENVIRONMENT != development`.
- [ ] Add a `/metrics` endpoint with basic request and job-state counters.
- [ ] Update `AUDIT_REPORT.md` risk #1 once cookie-only auth is complete.
- [ ] Compute a real reliability score (completion rate over last N jobs) in `_score_candidates` instead of the constant `0.5`.

---

## 7. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Launching with simulated inference only | Certain today | Critical | Phase A |
| Provider returns fake/low-quality output | High once real | High | Challenges + redundancy + slashing |
| XSS steals tokens from `localStorage` | Low–medium | High | Remove mirror, keep CSP |
| Ledger drift goes unnoticed | Low | Critical | Invariant alerts + nightly reconcile |
| Rate-limit bypass with multiple replicas | Medium | Medium | Require Redis |
| Contract griefing / replay | Medium (if deployed) | High | Contract fixes + audit |
| Scheduler routes to unreliable machines | High | Medium | Real reliability scoring |
| Documentation drift confuses contributors | High | Low | Consolidate docs, single source of truth |

---

## 8. Definition of "production ready"

- [ ] Real model inference with streaming, billed on measured usage.
- [ ] Result-integrity checks running on live traffic.
- [ ] No credentials in JS-accessible storage; CSP + cookie auth + CSRF verified by tests.
- [ ] Metrics, alerts, runbooks, backups and a tested restore.
- [ ] Postgres-backed CI covering concurrency and ledger properties.
- [ ] Redis-backed rate limiting enforced in production.
- [ ] Load test baselines recorded and capacity plan written.
- [ ] External security review (and contract audit if on-chain rails are enabled).
- [ ] Documentation consolidated and README matches the repository.

---

_Prepared for the ComputeCoin team. Items tagged **[to confirm]** should be validated against the code before scheduling work._
