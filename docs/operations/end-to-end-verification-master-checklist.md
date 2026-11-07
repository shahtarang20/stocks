# End-to-end verification: master checklist and master prompt

_Status date: 2026-11-06 · Owner: engineering / QA · Status: **not started** ·
Baseline: `main` at `e5a9256`. Almost all code from the earlier plans was written **without ever being run**.
This document is how we find out what actually works._

**Purpose.** One list that proves, step by step, whether the whole ComputeCoin project runs end to end: build, database,
API, worker, web, storage, agent, jobs, billing, privacy, admin tools, and (where hardware allows) real GPUs and Windows.

| Related | Why |
|---|---|
| [production-readiness-master-plan.md](production-readiness-master-plan.md) | Packages P1–P6 and their hardware items |
| [remaining-code-work-master-plan.md](remaining-code-work-master-plan.md) | Packages A, B, C and the locked decisions L1–L11 |
| [cross-platform-windows-linux-master-plan.md](cross-platform-windows-linux-master-plan.md) | Windows packages W0–W15 |
| [external-interfaces-checked.md](external-interfaces-checked.md) | Vendor interfaces already checked or still unverified |

---

## 1. Can the whole project run on one laptop?

**Short answer: most of it, not all of it.** A single Linux laptop can run the entire software stack and prove the logic
end to end. Some things need other hardware, other machines or cloud accounts, and those are marked so nobody mistakes a
laptop pass for proof of them.

### 1.1 Environment codes used in every checklist row

| Code | Needs | Runs on one laptop? |
|---|---|---|
| **L** | Only a Linux laptop with Docker, Python 3.12, Node 22 | **Yes** |
| **L+** | Laptop plus a free tool installed in the project's `.tools/` folder (software TPM `swtpm`, a local Hardhat chain, gVisor `runsc`) | **Yes** |
| **G** | An NVIDIA GPU on a Linux machine with the container toolkit | Only if the laptop has one |
| **W** | A Windows PC (with an NVIDIA GPU for the host items) | No |
| **M** | Two or more machines | No |
| **C** | Cloud accounts or the deployed environment (Railway, Vercel, R2, Stripe test mode, Resend) | Partly: needs internet and accounts |
| **H** | Special hardware (real TPM boot, AMD SEV-SNP or Intel TDX, NVIDIA H100 in confidential mode, an AMD GPU) | No |
| **T** | Elapsed time (soak runs, rotations, expiries) | Needs patience, not hardware |

### 1.2 What this laptop has (checked 2026-11-06)

| Item | Status |
|---|---|
| CPU / RAM / free disk | 12 cores, 31 GB RAM, 82 GB free: enough for the stack |
| Docker, Docker Compose | present (confirm the daemon is running) |
| Node 22, npm, Python 3, `psql` | present |
| Project virtual environments | `apps/api/.venv`, `provider-agent/.venv` present |
| NVIDIA GPU (`nvidia-smi`) | **absent** → all **G** rows cannot be run here |
| `swtpm` (software TPM) | **absent** → install into `.tools/` for **L+** TPM rows |
| `runsc` (gVisor) | **absent** → needed for the gVisor escape tests |
| Local Redis binary | absent, use the compose Redis container |

### 1.3 Coverage by environment (what a laptop can and cannot prove)

| Area | One laptop proves | Needs more |
|---|---|---|
| Build, migrations, all test suites | **everything** (L) | none |
| API, worker, web, database, Redis, object storage (MinIO) | **everything** (L) | none |
| Customer journey with the simulated executor | **everything** (L) | none |
| Money: ledger, holds, settlement logic, deposits and withdrawals on a local chain | **everything** (L+) | real chain and audit for production |
| Provider agent against a local API (enrol, heartbeat, claim, result) | **everything** (L) | none |
| Real containers on the CPU (hardened argv, egress allowlist, escape tests) | **yes** (L, L+ for gVisor) | none |
| Real GPU jobs, real kits, determinism, thermal | no | **G** |
| Privacy tiers with the software attestation and the software TPM | **yes** (L+) | real hardware for real proof (**H**) |
| Multi-node, failover across hosts, services on a second host | no | **M** |
| Windows host, installer, WSL2 runtime | no | **W** |
| Deployed site, real R2 upload from a browser, Stripe test payments, email | no | **C** |
| Load, capacity, 24-hour soak | partly | **T**, **M** |

**Bottom line:** the laptop can reach roughly **70–80%** of the checklist. Run it first. The rest is a short list of
targeted sessions on a GPU machine, a Windows machine, a second host and the deployed environment (section 12).

---

## 2. Rules for running this checklist

1. Record the **date, commit hash, machine and result** in the Result column. Never tick without a result.
2. A failing item becomes an entry in the **defect log** (section 14). Do not skip it and do not edit the check to make it pass.
3. Commands below come from the repository's own Makefile, plans and test files. They have **not been run**. If a
   command is wrong, fix the command in this file and note the correction.
4. Use throwaway data, a throwaway database and fresh random keys. Never point a local run at production or the staging database.
5. Never paste real secrets into this file, tickets or chat. Secrets stay in environment variables or `.env` files that are git-ignored.
6. Money code (ledger, pricing, billing, settlement, contracts) is read-only while testing. A failure there is reported, not patched on the spot.
7. Run phases in order. A failed gate in phases 0–2 blocks everything after it.

---

## 3. Phase 0: prepare the laptop

### 3.0 SAFETY PREFLIGHT (a human does this before any agent starts)

**Why this is mandatory.** `apps/api/.env` on this laptop can hold REAL credentials (a hosted Postgres database, a hosted
Redis, an email key). The API, Alembic, the worker and `scripts/seed_dev.py` read that file automatically. Without this
step, `alembic downgrade base`, the seed script or the worker would run against the real database and could delete data
or send real email. The settings file loads `.env` from the folder you run in, so this is not hypothetical.

1. **Move the real credentials aside.** Rename, never delete: `mv apps/api/.env apps/api/.env.disabled`. Restore it
   after the run. Do the same for any other file holding live keys (for example `contracts/.env`).
2. **Create a local-only environment file** (git-ignored location: `.tools/e2e.local.env`):
   ```
   DATABASE_URL=sqlite+aiosqlite:///./e2e.db
   REDIS_URL=redis://localhost:6379/0
   ENVIRONMENT=development
   EMAIL_PROVIDER=console
   RESEND_API_KEY=
   STRIPE_SECRET_KEY=
   SENTRY_DSN=
   AUTH_SECRET=local-e2e-auth-secret-not-for-production
   SESSION_SECRET=local-e2e-session-secret-not-for-production
   ```
   Load it in every shell before running anything: `set -a; source .tools/e2e.local.env; set +a`.
   For Postgres runs, point `DATABASE_URL` at the local compose database (`localhost`), never at a hosted host.
3. **Run the preflight and read the result:** `apps/api/.venv/bin/python scripts/e2e_preflight.py`.
   It must print `PREFLIGHT OK`. It prints only the kind and host of each target, never a secret. Any `UNSAFE` line is a stop.
4. **Limit what the agent can do.** Run the agent in a permission mode that asks before shell commands, or in a
   separate OS user or VM. Make sure no logged-in `railway`, `vercel` or `gh` session and no git push credentials are
   reachable if you do not want the agent to be able to use them. The prompt forbids pushing, but a tool permission is the real guard.
5. **Know what an agent will still do on its own:** install packages from the internet (pip, npm), pull and run container
   images, start local services, write under the project folder and `.tools/`, and run attack probes against its own throwaway containers.


| ID | Check | How | Pass when | Env | Result |
|---|---|---|---|---|---|
| E0.0 | **Safety preflight passes** | `apps/api/.venv/bin/python scripts/e2e_preflight.py` (after section 3.0) | prints `PREFLIGHT OK`; repeat at the start of every phase and after any environment change | L | |
| E0.1 | Tool versions recorded | `python3 --version`, `node --version`, `docker --version`, `docker compose version` | Python 3.12+, Node 22, Docker running (`docker ps` works) | L | |
| E0.2 | Clean checkout at the right commit | `git status`, `git log -1` | Working tree matches the commit under test | L | |
| E0.3 | API dependencies installed | `make api-install` | No error; `jsonschema`, `signxml`, `boto3`, `Pillow`, `numpy`, `scikit-image`, `imagehash`, `redis`, `websockets` import | L | |
| E0.4 | Agent dependencies installed | `cd provider-agent && .venv/bin/pip install -e ".[dev]"` | No error; `psutil` imports | L | |
| E0.5 | SDK and web dependencies installed | `cd sdk/typescript && npm install`; `cd apps/web && npm install`; `cd contracts && npm install` | No error | L | |
| E0.6 | Optional tools installed under `.tools/` | `swtpm`, `tpm2-tools`, `runsc`, a Hardhat chain (`npx hardhat node`) | Each prints its version | L+ | |
| E0.7 | Python import smoke (catches import cycles first) | `cd apps/api && .venv/bin/python -c "import app.main"` | Exits 0; **the new inference, kit, fan-out, session and service modules import without a cycle** | L | |

---

## 4. Phase 1: static and build gates

| ID | Check | Command | Pass when | Env | Result |
|---|---|---|---|---|---|
| E1.1 | Migration chain is linear | parse `apps/api/alembic/versions/` or `alembic heads` | exactly one head (`0042_agent_releases` at this commit) | L | |
| E1.2 | Migrations apply on SQLite | preflight OK first, then `cd apps/api && DATABASE_URL=sqlite+aiosqlite:///./e2e.db .venv/bin/alembic upgrade head` | success | L | |
| E1.3 | Migrations round-trip | preflight OK, then `DATABASE_URL=sqlite+aiosqlite:///./e2e.db alembic downgrade base` then `upgrade head` (**never** run a downgrade without an explicit local `DATABASE_URL`) | both succeed | L | |
| E1.4 | Migrations apply on Postgres 16 | start `make infra`, preflight OK with `DATABASE_URL` pointing at `localhost`, then `alembic upgrade head` | success | L | |
| E1.5 | API suite | `cd apps/api && .venv/bin/python -m pytest -q` | all pass; record counts against the earlier baseline (671 passed, 41 skipped) | L | |
| E1.6 | API suite on Postgres | `TEST_DATABASE_URL=postgresql+asyncpg://… .venv/bin/python -m pytest -q -rs` | all pass; the `postgres_only` concurrency tests run, not skip | L | |
| E1.7 | Agent suite | `cd provider-agent && .venv/bin/python -m pytest -q` | all pass (baseline 138) | L | |
| E1.8 | Platform boundary lint | included in E1.7 (`test_platform_boundary.py`) | no Linux-only call outside `platform/` | L | |
| E1.9 | Python SDK | `cd sdk/python && ../../apps/api/.venv/bin/python -m pytest -q tests` | all pass (baseline 76) | L | |
| E1.10 | TypeScript SDK | `cd sdk/typescript && npx vitest run && npx tsc --noEmit` | all pass (baseline 22), no type errors | L | |
| E1.11 | Guardian and witness | `apps/api/.venv/bin/python -m pytest -q apps/guardian apps/witness` | all pass (baseline 28) | L | |
| E1.12 | End-to-end privacy tests | `apps/api/.venv/bin/python -m pytest -q -rs tests/e2e` | pass; skips are explained (TPM test needs `swtpm`) | L / L+ | |
| E1.13 | Web | `cd apps/web && npx vitest run && npm run typecheck && npx next lint && npm run build` | all pass; axe tests report zero serious or critical violations | L | |
| E1.14 | Contrast check | `cd apps/web && npm run check:contrast` | every token pair ≥ 4.5:1 | L | |
| E1.15 | Contracts | `cd contracts && npx hardhat test` | all pass (baseline 33) | L | |
| E1.16 | Kit gates | `python3 scripts/kit_ci.py check` and `pytest kits/tests` | every `kit.json` passes (digests may be `PENDING` by design) | L | |
| E1.17 | Fan-out vectors | `python scripts/gen_fanout_vectors.py --write && python scripts/gen_fanout_vectors.py --check-app`, then the vector tests | app and script agree; the two skipped JSON comparison tests now run and pass | L | |
| E1.18 | Secret scan | `gitleaks detect` or the repo's CI scan | no findings | L | |
| E1.19 | Existing-tests-changed review | list tests edited under rule L6 (streaming refusal, attestation helpers, stream relay pad) | each change justified | L | |

**Gate:** E1.1–E1.7, E1.13 and E1.15 must pass before phase 2.

---

## 5. Phase 2: bring up the local stack

Ports: compose serves the API on **8000** and the web on **3000**. If `apps/web/.env.local` pins another port (it
pinned 8010 earlier), either change it or run the API on that port. A mismatch produces the "Unable to reach the API" error.

| ID | Check | How | Pass when | Env | Result |
|---|---|---|---|---|---|
| E2.1 | Backing services up | `make infra` (db, redis, minio) | `docker compose ps` shows all healthy | L | |
| E2.2 | MinIO bucket exists | open `http://localhost:9001` (login from compose), create bucket `computecoin-data`, add a CORS rule for `http://localhost:3000` (GET, PUT, HEAD) | bucket visible | L | |
| E2.3 | API starts | preflight OK, then `make api-migrate && make api-run` (or `docker compose up api`) | `curl localhost:8000/health` returns `status: ok`, `db: up`, `redis: up` | L | |
| E2.4 | Worker starts | `cd apps/api && .venv/bin/python -m app.worker` | logs show sweeps without tracebacks | L | |
| E2.5 | Data plane enabled locally | set `DATA_PLANE_ENABLED=true`, `DATA_PLANE_ENDPOINT=http://localhost:9000`, bucket, MinIO keys, `DATA_PLANE_ADDRESSING_STYLE=path`; restart API and worker | `/v1/data/files` returns 401 (route live, login required) | L | |
| E2.6 | Web starts | `make web-dev` with `NEXT_PUBLIC_API_BASE_URL` pointing at the API | `curl -I localhost:3000/login` returns 200 | L | |
| E2.7 | CORS from the web origin | preflight to `/auth/register` with `Origin: http://localhost:3000` | 200 and `access-control-allow-origin` echoes the origin | L | |
| E2.8 | Smoke script | `./scripts/smoke-test.sh http://localhost:8000` | all steps pass | L | |
| E2.9 | Seed data | preflight OK, then `apps/api/.venv/bin/python scripts/seed_dev.py` | seeds a provider, machine, model without error | L | |
| E2.10 | Optional: local chain | in `contracts/`: `npx hardhat node`; deploy token and settlement scripts; set `CHAIN_RPC_URL` and addresses | contracts deployed, API settlement and reconciliation read them | L+ | |
| E2.11 | Optional: guardian and witness services | start per their READMEs; set the privacy variables with throwaway keys | `/health` of each is ok | L+ | |

---

## 6. Phase 3: customer journeys

Use two test accounts (customer, admin) created through the real sign-up and email-verification flow (console email
provider locally, so the link is in the API log).

| ID | Journey | Steps | Pass when | Env | Result |
|---|---|---|---|---|---|
| E3.1 | Sign up | open `/register`, tick the acceptable-use box, submit | account created; the policy version is stored; sign-up without the box is blocked | L | |
| E3.2 | Verify email and log in | follow the console link, then log in | session works; wrong password gives "Invalid credentials" | L | |
| E3.3 | Credits | check the credit balance | test credits shown; ledger entries balance | L | |
| E3.4 | Marketplace | browse GPU listings | list and detail pages load | L | |
| E3.5 | Plain v1 job (simulated) | submit a text-generation job with the simulated model | job moves queued → running → completed; output shown; credits captured; hold released | L | |
| E3.6 | Streaming chat (simulated) | `POST /v1/chat/completions` with `stream: true` | SSE chunks arrive in order, end with `[DONE]`; text equals the non-streaming answer | L | |
| E3.7 | Private chat streaming refused | same request with a private tier while the flag is off | 400 `streaming_not_supported_for_private` | L | |
| E3.8 | Kit catalogue | open the Kits page | published kits listed (publish one first, see E5.3) | L | |
| E3.9 | Kit form from schema | open a kit | form fields, defaults and validation match the kit's schema | L | |
| E3.10 | Resumable upload | upload a 200 MB file through the wizard; interrupt the network midway; resume | the upload resumes; a deliberately corrupted part is re-sent; the final hash matches | L | |
| E3.11 | Deduplication | upload the same file again | no second upload; stored once | L | |
| E3.12 | Quote | request a price estimate | a low/expected/high range capped by the maximum cost (feature flag on) | L | |
| E3.13 | Kit job (simulated agent) | submit a kit job against a local simulated host | job placed; inputs staged; outputs uploaded to MinIO; hashes verified | L | |
| E3.14 | Download | open the job result and download | expiring link works and then expires; wrong user gets 404 | L | |
| E3.15 | Fan-out | submit a fan-out of 20 parts | parts run, one forced failure re-runs alone, merged output is in order, no double billing | L | |
| E3.16 | Pipeline | submit a 3-step pipeline; fail step 2; restart | step 1 not re-run or re-billed; step 2 and 3 complete | L | |
| E3.17 | Session | open a terminal session | encrypted relay carries keystrokes; closing or idling stops billing | L | |
| E3.18 | Session HTTP proxy | open a notebook session through the proxy page | the page loads over the channel (needs a separate origin or the local dev flag) | L | |
| E3.19 | Service endpoint | deploy a small service; call it; stop a replica | requests are answered; failover completes within the target | L (M for a second host) | |
| E3.20 | BYOC | submit a bring-your-own-container job | refused while no scanner is configured; with Trivy configured, scanned and isolated | L / L+ | |
| E3.21 | Cancel | cancel a running job | holds released; the agent stops the container | L | |
| E3.22 | Account deletion and export | use the profile page | data removed or exported as designed | L | |
| E3.23 | Spend limits | set a low limit; submit a larger job | refused with `spend_limit_exceeded`; nothing held | L | |
| E3.24 | Rate limits and abuse | burst requests | 429 where designed; abuse flag created in observe-only mode | L | |

---

## 7. Phase 4: host (provider) journeys

| ID | Journey | Steps | Pass when | Env | Result |
|---|---|---|---|---|---|
| E4.1 | Enrol | create an enrolment token in the UI; `computecoin-agent enroll --token … --server http://localhost:8000` | machine appears; the credential file is 0600 | L | |
| E4.2 | Detect hardware | `computecoin-agent detect` | JSON report; no crash on a GPU-less laptop | L | |
| E4.3 | Heartbeat | `computecoin-agent run --iterations 3` | heartbeat accepted; `platform` and `capabilities` stored; admin fleet view shows them | L | |
| E4.4 | Claim and run (simulated executor) | `computecoin-agent work --once` after submitting a job | claim → result → settlement; receipt idempotent on retry | L | |
| E4.5 | Hardened container run (CPU) | run a tiny digest-pinned image through the real Docker executor | argv shows read-only rootfs, dropped capabilities, non-root, no network, memory and PID limits | L | |
| E4.6 | Egress allowlist | job with `network_policy` to one host | allowed host reachable, any other refused and logged; metadata address blocked; private tier refused | L | |
| E4.7 | Escape tests | `COMPUTECOIN_ESCAPE_TESTS=1 COMPUTECOIN_ESCAPE_IMAGE=<digest-pinned image> pytest provider-agent/tests/escape` | every attempt (rootfs write, mount, `docker.sock`, host PID and net, `/proc/1/environ`, devices, ptrace, capabilities) fails | L | |
| E4.8 | gVisor path | same with the gVisor runtime | runs under `runsc`; refused if absent | L+ | |
| E4.9 | Thermal capping (fake readings) | feed readings above the cap and the hard stop | claims stop and resume with hysteresis; the job fails `thermal_shutdown` with no charge | L | |
| E4.10 | Yield to the owner | fake foreground and full-screen signals | availability `yielding`; no claims; missing signals block | L | |
| E4.11 | Control channel | start the runner with a control socket; send drain and stop | drain finishes the job; wrong token refused; no LAN listener | L | |
| E4.12 | Support bundle | `computecoin-agent support-bundle` | zip produced, scrubbed; planted secrets never appear | L | |
| E4.13 | Update flow with fakes | run the updater tests against a fake manifest | bad signature, bad hash, failed health check (rollback) and no-update-during-job all behave | L | |
| E4.14 | Real GPU job | `RUN_GPU_TESTS=1` plus a real kit run | valid output; two runs byte-identical | **G** | |
| E4.15 | Multi-GPU | 4-GPU job on one host | disjoint device sets for concurrent jobs | **G** | |
| E4.16 | Multi-node | two hosts in one approved group | rank 0 reachable, sibling cancel works | **M** | |

---

## 8. Phase 5: admin and operator journeys

| ID | Journey | Steps | Pass when | Env | Result |
|---|---|---|---|---|---|
| E5.1 | Admin login and console | log in as admin | admin pages load; non-admin gets 403 on admin routes | L | |
| E5.2 | Fleet view | open admin telemetry | machines with platform and capability badges; `throttled` card | L | |
| E5.3 | Publish a kit | generate a key (`scripts/sign_kit.py --gen-key`), set `KIT_SIGNING_PUBLIC_KEY`, sign and publish | kit visible; a tampered manifest, wrong key and DB-edited kit are refused | L | |
| E5.4 | Yank a kit | yank it | new jobs refused; running jobs unaffected | L | |
| E5.5 | Register an image digest | admin route for model images | per-runtime rows created; an IMAGE_HASH log entry appended | L | |
| E5.6 | Abuse admin | list flags, resolve, release a hold, take down a report | all admin-only; audited | L | |
| E5.7 | Release management | publish, pause, roll back an agent release | host manifest route returns the right ring; bad signature refused | L | |
| E5.8 | Stake sync | with a fake RPC or the live local-chain test | bonded stake mirrors the vault; mismatch marks `stake_unverified`; the admin stake route returns 409 when sync is on | L / L+ | |
| E5.9 | Metrics | set `OBSERVABILITY_METRICS_TOKEN`; scrape `/v1/ops/metrics` | 404 without the token, metrics with it; Windows gauges present | L | |
| E5.10 | Dashboards and alerts | load `infrastructure/observability` into local Prometheus and Grafana | rules parse; dashboard imports | L | |
| E5.11 | Audit log | perform sensitive actions | each has an audit row | L | |

---

## 9. Phase 6: money and ledger (read-only checks)

| ID | Check | How | Pass when | Env | Result |
|---|---|---|---|---|---|
| E6.1 | Ledger invariant | after phases 3–5, sum all debits and credits | equal | L | |
| E6.2 | Hold → capture → release | one completed, one cancelled, one failed job | balances correct; no leaked holds | L | |
| E6.3 | Idempotency | repeat a posting key; repeat a result submission | applied once | L | |
| E6.4 | Concurrency (Postgres) | the `postgres_only` tests | claims, lease recovery, key release and ledger postings correct under contention | L | |
| E6.5 | Quote cap | job costs ≤ the cap and ≤ the quote | holds | L | |
| E6.6 | Service per-request billing | accrue, close a batch, force a late request and a metering failure | tail carried forward; never overcharged; reconcile flags correct | L | |
| E6.7 | Chain reconciliation | local chain escrow vs the ledger | agree; an injected drift is reported | L+ | |
| E6.8 | Token deposits | send a test deposit on the local chain | credited once after confirmations | L+ | |
| E6.9 | Withdrawals off by default | request a withdrawal | refused while `WITHDRAWALS_ENABLED=false` | L | |
| E6.10 | Withdrawals on a local chain | `RUN_WITHDRAWALS_INTEGRATION=1` | payout, revert → refund, reorg, mismatch → pause, unlinked wallet refused | L+ | |
| E6.11 | Stripe test mode | test payment and webhook | credits added once | **C** | |
| E6.12 | Independent money review | external reviewer on `ledger.py` and the withdrawal code | report with no open critical finding | external | |

---

## 10. Phase 7: privacy and security

| ID | Check | How | Pass when | Env | Result |
|---|---|---|---|---|---|
| E7.1 | Software-attestation private job | `tests/e2e/test_proof_of_privacy_e2e.py` | job runs; result sealed to the customer key; receipt verifies offline | L | |
| E7.2 | Software TPM attestation | `tests/e2e/test_tpm_swtpm_e2e.py` with `swtpm` | verdict allowed for the sealed tier; a changed measurement is refused | L+ | |
| E7.3 | Sealed image boot (opt-in) | `COMPUTECOIN_SEALED_BOOT_TEST=1` | booted PCRs equal the prediction; one flipped byte changes them | L+ | |
| E7.4 | Guardian threshold | stop one guardian | release still works at the threshold; below it, refused | L+ | |
| E7.5 | Canary leak | trigger a canary | stake slashed, machine banned | L | |
| E7.6 | Transparency log | append, prove inclusion, check consistency, anchor on the local chain, witness co-signature | all proofs verify | L+ | |
| E7.7 | Private fan-out | enable `PRIVACY_FANOUT_ENABLED` with throwaway keys | per-part receipts and the job receipt verify; the platform never holds plaintext | L+ | |
| E7.8 | Private streaming | enable `PRIVACY_STREAMING_ENABLED` | chunks are sealed and padded; the relay refuses unpadded ones; the SDK decrypts | L+ | |
| E7.9 | Authentication hardening | CSRF, cookie flags, session expiry, password reset | as designed | L | |
| E7.10 | Authorization | cross-account access to jobs, files, sessions, services | always 404 or 403 | L | |
| E7.11 | Input fuzzing | `tests/qa/fuzz.py`, `fuzz_get.py` | no 500s | L | |
| E7.12 | Dependency audit | `pip-audit`, `npm audit` | no critical findings left untriaged | L | |
| E7.13 | Real hardware attestation | SEV-SNP or TDX plus NVIDIA CC evidence | verdict `confidential`; another machine's GPU report refused | **H** | |

---

## 11. Phase 8: quality, performance and accessibility

| ID | Check | How | Pass when | Env | Result |
|---|---|---|---|---|---|
| E8.1 | Browser flows | `tests/e2e-web`, `tests/browsers`, `tests/qa-customer`, `tests/plain-language` | pass on Chromium and Firefox | L | |
| E8.2 | Keyboard and screen-reader pass | tab through landing, sign-up, login, dashboard, new job, job page, marketplace, host pages; test with a screen reader | everything reachable; focus visible; errors announced; record in `qa2.md` | L | |
| E8.3 | Mobile layout | 375 px width | no horizontal scroll; monitoring and download usable | L | |
| E8.4 | Load test (local stack) | Locust scenarios in `tests/load` | record p50/p95/p99, error rate and throughput; fill `load-test-baseline.md`; set thresholds | L | |
| E8.5 | Soak (shortened) | 2 hours of mixed traffic | no memory growth, no stuck jobs, leases recover | L, T | |
| E8.6 | Restart resilience | kill API and worker mid-job | jobs recover; no double billing | L | |
| E8.7 | Backup and restore | dump and restore the database | the app works after restore | L | |

---

## 12. Phase 9: things one laptop cannot prove

Run these as separate sessions. Each row says what is needed.

| ID | Check | Needs | Pass when | Env | Result |
|---|---|---|---|---|---|
| E9.1 | Real GPU kit smoke on ≥ 3 GPU models | GPU machines | every kit passes its smoke spec | G | |
| E9.2 | Deterministic output across two runs, and across two hosts | two GPU hosts | byte-identical where the kit is deterministic | G, M | |
| E9.3 | Thermal and power limit on a real GPU | NVIDIA GPU | cap, hard stop and power limit work | G | |
| E9.4 | Windows requirements checker | Windows PC | clear pass or fail list | W | |
| E9.5 | Windows service lifecycle | Windows PC | installs, starts on boot, stops gracefully | W | |
| E9.6 | WSL2 runtime provisioning and GPU smoke test | Windows PC + NVIDIA GPU | the distro is created, hardened and passes the smoke test | W | |
| E9.7 | Windows isolation tests | Windows PC | a job cannot read `C:\`, call Windows interop or reach the LAN | W | |
| E9.8 | Windows end-to-end kit job and parity with Linux | Windows GPU PC + a Linux host | output hashes equal | W, M | |
| E9.9 | Installer: install, upgrade, rollback, uninstall | clean Windows VMs | no residue left | W | |
| E9.10 | Windows 24-hour soak with sleep and reboot events | Windows GPU PC | recovers and keeps heartbeating | W, T | |
| E9.11 | Multi-node on two hosts | two hosts, approved group | rendezvous and sibling cancel work | M | |
| E9.12 | Service failover to a second host | two hosts | failover inside the target time | M | |
| E9.13 | Real TPM boot of the Sealed OS image | a TPM 2.0 PC | PCRs equal the prediction | H | |
| E9.14 | AMD GPU job (ROCm) | an AMD GPU host | container-tier job completes | H | |
| E9.15 | Confidential tier on real hardware | SEV-SNP or TDX host with H100 CC | verdict and receipts valid | H | |
| E9.16 | Deployed site register and login | Vercel + Railway | works with CORS configured | C | |
| E9.17 | Deployed upload to R2 from a browser | R2 bucket, CORS | a large file uploads, resumes and verifies; the checksum behaviour is confirmed | C | |
| E9.18 | Real email delivery | Resend with a real sending domain | verification and reset mails arrive | C | |
| E9.19 | Image and kit builds in CI | GHCR, repository variables | images build with pinned digests and publish | C | |
| E9.20 | Kit signing ceremony | the real offline key | signed kits verify on the deployed API | C | |

---

## 13. Sign-off and go or no-go

| Gate | Required to pass |
|---|---|
| **G1 Build** | E1.1–E1.18 pass, with counts at or above the baseline |
| **G2 Stack** | E2.1–E2.9 pass |
| **G3 Customer** | E3.1–E3.17 and E3.21–E3.24 pass |
| **G4 Host** | E4.1–E4.13 pass |
| **G5 Admin** | E5.1–E5.9 pass |
| **G6 Money** | E6.1–E6.10 pass; E6.12 has no open critical finding |
| **G7 Security** | E7.1, E7.2, E7.9–E7.12 pass; no open critical defect |
| **G8 Quality** | E8.1–E8.4, E8.6 pass |
| **Demo go** | G1–G3 and G5 pass |
| **Beta go** | G1–G8 pass plus E9.1, E9.16, E9.17 |
| **Public go** | everything above, plus E9.2, E9.11–E9.13, E9.18–E9.20, an independent security review and legal sign-off |
| **Windows hosts go** | E9.4–E9.10 pass, plus the owner's decisions in the Windows plan |

A single open **critical** defect (data loss, money error, privacy leak, host escape, authentication bypass) means no-go for that gate.

---

## 14. Defect log

| # | ID of the failing check | Severity (critical / high / medium / low) | What happened | Owner | Fixed in | Re-test result |
|---|---|---|---|---|---|---|
| | | | | | | |

---

## 15. Run record

| Run | Date | Commit | Machine and OS | Phases run | Pass / fail counts | Notes |
|---|---|---|---|---|---|---|
| 1 | | | | | | |

---

## 16. Master prompt

Copy everything inside the box into the agent.

~~~text
MASTER PROMPT: ComputeCoin end-to-end verification

ROLE
You are a senior QA and release engineer working in /home/root509/stocks. Your job is to RUN the project and prove
what works. The plan is docs/operations/end-to-end-verification-master-checklist.md. Read it completely. The
locked decisions L1-L11 of docs/operations/remaining-code-work-master-plan.md still apply.

GROUND RULES
0. FIRST, before anything else: confirm section 3.0 (safety preflight) was done by the human. Run
   apps/api/.venv/bin/python scripts/e2e_preflight.py. If it does not print PREFLIGHT OK, STOP and report. Run it again at
   the start of every phase and after any change to the environment. NEVER run a migration, a downgrade, the API, the
   worker, scripts/seed_dev.py or any script that opens the database unless the preflight passed in the same shell, and
   always pass an explicit local DATABASE_URL to alembic.
0b. NEVER open, print, copy, move or summarise any .env file, .env.disabled, credential, token or key. Do not read
    apps/api/.env.disabled. Do not run railway, vercel, gh, aws, stripe or wrangler commands or call any hosted URL.
0c. Docker: only docker compose for this project's own services, and docker run/pull for the named probe or kit
    images in the checklist. Never docker system prune, docker volume rm or docker rm on containers or volumes that
    this project did not create. Never mount the Docker socket or the home directory into a container.
0d. Deletions: only inside this repository, .tools/ and files you created in this run. Never rm -rf outside them.
1. Work ONLY inside /home/root509/stocks. Never touch another project. Never install anything system-wide; put tools
   in the git-ignored .tools/ folder or the project environments.
2. This codebase was written without ever being run. Expect import cycles, wrong names, migration errors,
   SQLAlchemy and Postgres quirks, and tests that disagree with the code. Record every failure; do not hide it.
3. Run phases in order. Do not start a phase until the previous gate passes. Report a failed gate and fix the cause.
4. Fix defects at the root cause, with the smallest change. Add or correct a test that fails without the fix.
   Do NOT weaken or delete a test to make it pass, and do not change a check in the checklist to make it pass.
5. Money code (ledger, pricing, billing, settlement, contracts) is READ-ONLY. If a check fails there, stop and
   report it to the owner with the evidence; do not patch it.
6. Use throwaway databases, throwaway keys and test data only. Never connect to the production or staging database,
   Railway, Vercel or R2 unless the owner explicitly says so for a named row. Never print or commit a secret.
   Rows in section 12 marked C (cloud) are run by the owner, not by you: mark them 'not run: owner session'.
7. Skip rows whose environment code (L+, G, W, M, H, C, T) is not available on this machine, mark them "not run:
   needs <code>", and list them in the final report. Never mark such a row as passed.
8. For every row: record date, commit hash, command run, and result in the Result column. A pass needs evidence
   (command output, file or log excerpt). A row without evidence stays unticked.
9. One agent edits the repository at a time. Before every edit confirm the file's modification time has not
   changed since you read it; if it has, stop and report.
10. Never commit or push without the owner's instruction; if on main, branch first.
11. After each phase run the relevant full suites again and compare counts with the baseline in the checklist.
12. At the end, write the run record (section 15), the defect log (section 14) and the gate table (section 13)
    with honest statuses, then WAIT for review.

TRAPS ALREADY HIT IN THIS CODEBASE
- Async SQLAlchemy cannot lazy-load: query, never touch relationship attributes on objects loaded without
  eager loading.
- Raising HTTPException rolls back the request's writes; return a JSONResponse when the failure itself must
  be recorded.
- Never read a security claim from an unsigned field next to signed bytes.
- Mocks follow the vendor's documented API, never the code's assumptions.
- computecoin_agent/platform shadows the stdlib platform module if that folder is on sys.path: always run from
  provider-agent/ or the repo root, never from inside computecoin_agent/.
- The web app's .env.local may pin a different API port than the documented 8000: a mismatch shows as
  "Unable to reach the API".
- apps/api/.env is read automatically from the folder you run in and may hold live credentials. The preflight
  (scripts/e2e_preflight.py) exists because of this. Alembic, the worker and seed_dev.py all use it.

REPORT FORMAT (after each phase)
Rows run and their results, rows skipped and why, defects found (ID, severity, evidence), fixes made (files and
tests), suite counts before and after, and what is still unverified.
~~~
