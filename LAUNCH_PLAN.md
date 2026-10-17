# ComputeCoin — Production Launch Plan (Master Playbook)

> Turns the 5 launch blockers into an executable plan: objectives, acceptance
> criteria, who does what, step-by-step work, and a ready-to-run **master prompt**
> per workstream. Pair with [AUDIT_REPORT.md](AUDIT_REPORT.md).

## Decisions locked
| Decision | Choice |
|---|---|
| Crypto token / on-chain settlement | **In scope** — ship it, but mainnet still gated on external audit + legal |
| Payment rail | **Stripe** |
| Hosting | **Vercel** (web) · **Railway** (API + workers) · **Supabase** (Postgres + storage) · **Upstash** (Redis) |
| First track to build | **Audit items** (httpOnly cookies + contract fixes) |

## Current status (baseline)
Tests green: API 141 · provider-agent 60 · SDK 27/20 · contracts 11 · web builds.
Audit Critical→Low remediated and pushed (`main @ ee7b87e`). Execution is still
**simulated**, money is **test credits**, chain is **local (31337)**, and nothing
is **deployed**. This plan closes those gaps.

## Legend
🤖 = I build it (code, in-repo) · 👤 = you provide (account/credential/decision) ·
🏛️ = external (audit firm / legal) · ✅ = acceptance criterion.

---

# Workstream A — Deferred audit items (START HERE, zero deps)

**Objective:** remove the remaining security gaps that need no external resources.

### A1. httpOnly-cookie auth (replace localStorage tokens)
🤖 Backend: on login/refresh, set `access` + `refresh` as **httpOnly, Secure,
SameSite=Lax** cookies; add CSRF protection (double-submit token or SameSite
strict on mutations); accept either `Authorization: Bearer` (SDK) **or** cookie
(web). 🤖 Frontend: stop reading/writing tokens in JS; rely on cookies; drop the
`TODO(security)` localStorage path. 🤖 Tests: login sets cookies; cookie-auth
request works; no token in any JS-readable storage; CSRF rejected without token.
- ✅ No access/refresh token is stored in `localStorage`/JS.
- ✅ Existing SDK `Bearer` auth still works (back-compat).

### A2. On-chain ↔ off-chain ledger parity (make it real)
🤖 `get_reconciliation_summary` must **cross-check** recorded on-chain escrow
settled/refunded amounts against the corresponding ledger postings (or, if they
stay parallel rails, rename the field and document it). 🤖 `confirm_settlement_tx`
/ `confirm_refund_tx` should verify the claimed amounts against the escrow, not
trust client fields blindly.
- ✅ `reconciliation.imbalance == 0` reflects a real chain↔ledger comparison, with a test that detects an injected mismatch.

### A3. On-chain jobId squatting
🤖 Key escrows by `keccak256(customer, jobId)` (contract) + matching backend
derivation, so a third party can't pre-occupy another customer's jobId. 🤖 Update
contract tests.
- ✅ A non-owner cannot block a job's escrow; test proves it.

**Master prompt (A):**
> Implement Workstream A of LAUNCH_PLAN.md in the ComputeCoin repo. (A1) Migrate web
> auth to httpOnly/Secure/SameSite cookies set by the FastAPI backend on
> login/refresh, add CSRF protection, keep `Authorization: Bearer` working for the
> SDKs, and remove all localStorage token usage in apps/web; add tests. (A2) Make
> settlement reconciliation a real on-chain↔ledger cross-check and verify amounts
> in confirm_settlement/confirm_refund; add a mismatch-detection test. (A3) Key
> on-chain escrows by (customer, jobId) in the contract + backend and update tests.
> Keep all existing suites green; show diffs + real test output; do not weaken any
> check to pass a test.

---

# Workstream B — Real GPU execution (spec acceptance #7)

**Objective:** prove one real model runs on a real GPU through `DockerExecutor`.

🤖 Make `DockerExecutor` fully invocable (pinned image digest, `--gpus`, hardened
flags already specified in ADR-0003); add a **real-hardware integration test**
gated behind an env flag / GPU+Docker presence so CI stays CPU-only; write
`docs/operations/gpu-validation-runbook.md`. 🤖 Pick an initial supported model +
container image (e.g. a small vLLM or HF text-generation image), pin its digest in
the model registry. 👤 Provide a **GPU host** (your box or cloud: RunPod / Lambda
/ AWS g5) with NVIDIA driver + Docker + NVIDIA Container Toolkit, and run the agent
there: `computecoin-agent enroll … && computecoin-agent work --server <api>`.
- ✅ A text-generation job submitted via the API executes on a real GPU and returns real output (`execution_mode: "hardware"`), artifact downloadable, metrics real.
- ✅ Verification passes; the job is billed on **actual** metered runtime.

**Master prompt (B):**
> Make ComputeCoin's provider-agent DockerExecutor run a real GPU text-generation
> workload end to end. Finalize the hardened `docker run` invocation, register a
> real pinned model image digest, add a GPU-gated integration test (skipped without
> GPU+Docker), and write docs/operations/gpu-validation-runbook.md covering driver
> + NVIDIA Container Toolkit setup, enroll, `work`, and validating one real job
> (execution_mode "hardware", real artifact + metrics, billed on actual runtime).
> Keep CPU-only CI green.

---

# Workstream C — Real payment rail (Stripe)

**Objective:** customers actually buy credits; the ledger stays the source of truth.

🤖 Implement a `StripePaymentProvider` behind the existing billing interface:
Checkout Session / PaymentIntent to buy credits → **signed webhook** (`checkout.
session.completed` / `payment_intent.succeeded`) → **idempotent** `credit_purchase`
ledger posting (idempotency key = Stripe event id). Keep `simulated` as the default
for dev/test. 🤖 Reconciliation/refund path through Stripe refunds. 🤖 Tests with a
mocked Stripe client + webhook-signature verification. 👤 Stripe account + **test**
secret/publishable keys + webhook signing secret; later, live keys.
- ✅ A test-mode purchase credits the customer exactly once (replayed webhook = no double credit).
- ✅ Webhook signature is verified; unsigned/forged events rejected.
- ✅ Failed/disputed payments never leave phantom credits.

**Master prompt (C):**
> Add a Stripe payment rail to ComputeCoin billing behind the existing provider
> interface (keep `simulated` as fallback). Build credit purchase via Checkout/
> PaymentIntent and an idempotent, signature-verified webhook that posts a
> `credit_purchase` ledger transaction keyed by the Stripe event id; wire refunds
> through Stripe. Add tests with a mocked Stripe client proving: single credit on
> replayed webhook, rejected forged signatures, no phantom credits on failure.
> Config via env (STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, PAYMENT_PROVIDER=stripe).

---

# Workstream D — Blockchain to public testnet (Sepolia), audit-gated for mainnet

**Objective:** real testnet deployment + readiness for an external audit.

🤖 Hardhat deploy scripts + **Sepolia** network config (RPC + deployer via env, no
keys in repo); deploy `ComputeCoinToken` + `ComputeSettlement`; wire backend
`SETTLEMENT_CHAIN_ID`/addresses via env; multisig/timelock for owner + platform
signer (recommended) ; `docs/operations/contract-deploy-runbook.md`. 🤖 Apply the
A3 jobId-key fix before deploy. 👤 Sepolia RPC (Alchemy/Infura) + a funded testnet
deployer key (env only) + a multisig (e.g. Safe) for owner. 🏛️ **Independent
smart-contract audit + legal review before mainnet or any public token.**
- ✅ Contracts deployed + verified on Sepolia; a full lock→voucher→settle and a
  refund run on testnet and reconcile with the off-chain ledger.
- ⛔ **Do not deploy to mainnet / launch a tradable token until 🏛️ audit + legal clear it.**

**Master prompt (D):**
> Prepare ComputeCoin contracts for a public testnet launch: Hardhat deploy +
> verify scripts for Sepolia (RPC + deployer key from env, never committed), owner
> set to a multisig and platform signer behind a timelock, backend env wiring for
> the deployed addresses/chain id, and docs/operations/contract-deploy-runbook.md.
> Include an end-to-end testnet script: lock budget → issue voucher → settleJob →
> reconcile with the ledger, and a refund path. Do NOT target mainnet.

---

# Workstream E — Real deployment (Vercel + Railway + Supabase + Upstash)

**Objective:** stand up staging, then production, with monitoring and real email.

🤖 Production `docker-compose` + Railway service config (API, scheduler/worker,
sweeper) ; Vercel project config for `apps/web` ; Supabase (Postgres + storage)
and Upstash (Redis) connection wiring via env ; run Alembic migrations on deploy ;
**real email provider** (e.g. Resend/SES) behind the existing email interface
(stop returning dev tokens) ; `/health` + structured logging + error monitoring
(Sentry) + alerting hooks ; CI deploy workflow ; `docs/operations/deploy-runbook.md`
and a **go-live checklist**. 👤 Accounts (Vercel, Railway, Supabase, Upstash, email
provider, Sentry) + a **domain** + all production secrets (the C3 boot-guard
enforces strong secrets). 🤖 A load/smoke test script against staging.
- ✅ Staging is reachable over HTTPS on your domain; migrations applied; `/health` green.
- ✅ Real email delivers verification/reset (no tokens in API responses in prod).
- ✅ Monitoring + alerts fire on errors; a basic load test passes.
- ✅ Backups enabled on Supabase; rollback procedure documented.

**Master prompt (E):**
> Make ComputeCoin deployable to Vercel (web) + Railway (API + scheduler/worker +
> recovery sweeper) + Supabase (Postgres + storage) + Upstash (Redis). Add prod
> docker-compose + Railway/Vercel configs, env-driven connection wiring, Alembic-
> on-deploy, a real email provider behind the email interface (no dev-token
> exposure in prod), /health + structured logging + Sentry + alerts, a CI deploy
> workflow, a staging smoke/load test, and docs/operations/deploy-runbook.md with a
> go-live checklist. Enforce ENVIRONMENT=production + strong secrets (boot guard).

---

# Recommended sequence
1. **A** (audit items) — now, no deps. 🤖
2. **E code** (deploy configs + email + monitoring) in parallel with A. 🤖 → then 👤 stand up **staging**.
3. **C** (Stripe) once 👤 test keys arrive. 🤖
4. **B** (real GPU) once 👤 a GPU host is available — validate one real job. 🤖+👤
5. **D** (Sepolia deploy) 🤖 → **then 🏛️ audit + legal before mainnet/token.**
6. Private **beta** (simulated exec + test credits) can go live after A+E staging; **full production** after B+C+D(testnet)+audit.

# What you need to provide (👤 checklist)
- [ ] Stripe account + test keys + webhook secret
- [ ] Vercel, Railway, Supabase, Upstash, Sentry accounts
- [ ] Email provider (Resend/SES) API key + sender domain
- [ ] A domain name
- [ ] A GPU host (own or cloud) with NVIDIA driver + Docker + Container Toolkit
- [ ] Sepolia RPC URL + funded testnet deployer key (for D) + a multisig (owner)
- [ ] 🏛️ Engage a smart-contract auditor + legal counsel before mainnet/token

# Go-live gates
**Private beta (safe soon):** A done · E staging up · execution + credits clearly
labelled as simulated/test.
**Full production:** A+C+E done · B validated on real GPU · D on testnet & reconciling
· 🏛️ audit + legal cleared for any mainnet/token · load test passed · backups + rollback tested.

# Risk register (top)
| Risk | Mitigation |
|---|---|
| Real GPU exec differs from simulator | Workstream B integration test on real hardware before public |
| Payment double-credit / fraud | Idempotent signed webhooks; ledger is source of truth (C) |
| Smart-contract exploit on mainnet | External audit + multisig/timelock; testnet first (D) |
| Secret leakage in prod | C3 boot guard + secrets manager; no secrets in repo |
| Data loss | Supabase backups + tested restore (E) |
