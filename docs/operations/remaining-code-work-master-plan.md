# Remaining code work: master plan, master prompt and master checklist

_Status date: 2026-10-29 · Owner: engineering · Status: **plan as written at the baseline; implementation in progress, see note** ·
Baseline: `main` at `33e2c00` (API 671 passed / 41 skipped, agent 138, Python SDK 76,
TypeScript SDK 22, guardian + witness 28, e2e 10, web 22, contracts 33)._

> **Implementation note (2026-11-05).** Most items below now have code in the working tree (uncommitted). Statically reviewed only: no checklist item in this plan is ticked, because a tick needs its proof (a passing test run, or for **(hardware)** items a recorded file from a real machine, R8) and none of that has been attached yet. Hardware, live-chain and GPU paths have not been run. Read the checklists below as the acceptance list still to be proven, not as a record of what exists. The text of the plan is kept as written at the baseline; where it says "today" or "not built", it describes the baseline.

This is the **single list of everything that is code, planned, and not built yet** (verified
against the repository on 2026-10-29). It leaves out what is not code: hardware test runs,
approvals, accounts, deployment, audit and legal work. Those appear only where they gate a
code item.

| Read first | Why |
|---|---|
| [production-readiness-master-plan.md](production-readiness-master-plan.md) | Full design and checklist for Track A (P1–P6) |
| [ADR-0006 Universal Workloads](../architecture/ADR-0006-universal-workloads.md) | Architecture and checkpoints for Track C |
| [ADR-0004 Token and ledger](../architecture/ADR-0004-token-and-ledger.md) | Owner decisions that gate B1 |
| [ADR-0003 Isolation](../architecture/ADR-0003-isolation.md) | Container hardening every item must keep |
| [ADR-0005 Proof-of-Privacy](../architecture/ADR-0005-proof-of-privacy.md) | Privacy tiers that every new mode must respect |

---

## 1. Scope

### Track A: production pieces (design lives in production-readiness-master-plan.md)

| ID | What | Size |
|---|---|---|
| A1 | Stake sync: on-chain vault → database stake (P1) | Small–medium |
| A2 | Certificate revocation, Intel TDX-module check, NVIDIA signed reference measurements (P2) | Medium |
| A3 | Real AI model images (CUDA + ROCm) and the real inference path (P3) = ADR-0006 WP-U0 | Large |
| A4 | Agent collects SEV-SNP, TDX and NVIDIA evidence (P4) | Medium |
| A5 | Sealed OS image build pipeline (P5) | Medium |
| A6 | AMD GPUs in the Sealed tiers (P6), after the owner's D5 decision | Small–medium |

### Track B: platform gaps (designed in section 5 of this document)

| ID | What | Where it was planned | Size |
|---|---|---|---|
| B1 | Token withdrawals | ADR-0004 (withdrawals not built) | Medium; **money code** |
| B2 | Streaming chat responses (`stream: true` is refused today) | PROJECT_REVIEW_AND_ROADMAP.md | Small–medium |
| B3 | CI and concurrency tests on Postgres | PROJECT_REVIEW_AND_ROADMAP.md | Small |
| B4 | Load tests and a capacity baseline | PROJECT_REVIEW_AND_ROADMAP.md | Small–medium |
| B5 | Per-job network allowlist (jobs always run with no network today) | executor.py docstring, ADR-0006 U3.3 | Small |
| B6 | GPU thermal capping on provider machines | qa.md | Small |
| B7 | Website accessibility | qa2.md | Small–medium |

### Track C: Universal Workloads (ADR-0006, "proposed, not built")

| ID | What | ADR estimate |
|---|---|---|
| U1 | Job Spec v2 and Kit Registry | 3–4 weeks |
| U2 | Data plane (large resumable uploads, dedup, scoped links) | 3–4 weeks |
| U3 | Agent runtime v2 (read-only inputs, caches, egress allowlist, checkpoints) | 4–5 weeks |
| U4 | Fan-out planner and merge (e.g. render frames across hosts) | 3 weeks |
| U5 | Kits wave 1: AI image, Blender, ffmpeg, upscaling | 4–6 weeks |
| U6 | Verification engine plug-ins (perceptual, PSNR/SSIM) | 3 weeks |
| U7 | Scheduler v2 (VRAM, cache affinity, multi-GPU) | 3–4 weeks |
| U8 | Bring-your-own-container (BYOC) | 3–4 weeks |
| U9 | Pipelines (DAG) | 2–3 weeks |
| U10 | Interactive sessions | 4–5 weeks |
| U11 | Services / endpoints | 4 weeks |
| U12 | Kits wave 2 and 3 | 6–10 weeks, rolling |
| U13 | Multi-GPU and datacenter multi-node | 3–5 weeks |
| U14 | Windows VM tier (optional) | 6–8 weeks |
| U15 | Pricing units and quotes | 2 weeks |
| U16 | Acceptable use, abuse and content safety | 3–4 weeks + legal |
| U17 | Web experience | 4–6 weeks |
| U18 | Privacy across all modes | with ADR-0005 work |
| U19 | Observability | 2 weeks |

(U0, real GPU execution, is A3.)

### Recommended order

1. **B3** Postgres CI (cheap, protects everything after it).
2. **A1**, **A2** (no hardware needed).
3. **B6**, **B5** (small; B5 is needed by U3 and U8).
4. **A3** (real outputs), then **B2** streaming (needs A3 for real tokens), then **B4** load tests (on real paths).
5. **A4**, **A5**, **A6** as hardware becomes available.
6. **B7** accessibility any time (independent).
7. **B1** only after the owner answers ADR-0004 section 5.
8. **Track C** after A3, in this order: U1 → U2 → U3 → U7 → U4 → U5 → U6 → U15 → U17 → U19 → U8 → U9 → U16 → U10 → U11 → U13 → U12 → U18 → U14 (optional).

---

## 2. Locked decisions (apply to every item)

| ID | Decision |
|---|---|
| **L1 Fail closed** | What is not proven is refused. No silent default that grants access, money, a tier or network. |
| **L2 One agent at a time** | Only one coding agent edits the repository at a time. Before every edit, check the file's modification time is unchanged since it was read; if not, STOP. |
| **L3 Money code needs approval** | Ledger, pricing, billing, settlement and `contracts/` change only inside B1 or U15, after the owner approves that package's design, with an independent review before merge. Everywhere else they are read-only. |
| **L4 Verify every external interface** | Every vendor API, file format, library, tool, image and URL is checked against current official documentation before use; the URL and date are recorded next to the code. Memory of an API is not evidence. |
| **L5 Tests prove behaviour** | Every behaviour change ships with a test that fails without it; each package ends with a bug re-insertion run (remove each new check, a named test fails, restore, checksums prove the restore). |
| **L6 Existing tests stay** | An existing test changes only when a locked decision requires it, with the reason in the commit. |
| **L7 No secrets** | No private key in code, images, fixtures, logs or test output; tests use fresh random keys. |
| **L8 Design note before big code** | Every Track C package, and B1, B2 and B5, starts with a short design note (interfaces, data model, security, tests) under `docs/architecture/` that the owner approves before code. |
| **L9 ADR-0006 principles** | Track C follows ADR-0006 P1–P10: one job model, pinned images, untrusted by default, data never relayed through the API, GPU-seconds as the billing unit, every workload verifiable, privacy tiers for every mode, fail safe, hosts outbound-only, v1 jobs unchanged. |
| **L10 No network by default** | Job containers have no network unless a validated allowlist is declared (B5); privacy-tier jobs never get network. |
| **L11 Stay in the project** | Work only inside `/home/root509/stocks`; never install anything system-wide; tools go in the git-ignored `.tools/` folder or project environments. |

---

## 3. Master prompt

Copy everything inside the box into the agent.

~~~text
MASTER PROMPT: ComputeCoin remaining code work (Tracks A, B, C)

ROLE
You are a senior engineer working in /home/root509/stocks. The plan is
docs/operations/remaining-code-work-master-plan.md. Read it fully, including locked decisions
L1–L11 (section 2). For Track A also read docs/operations/production-readiness-master-plan.md
(its R1–R12 apply too). For Track C also read docs/architecture/ADR-0006-universal-workloads.md.
If any decision looks wrong, STOP and ask the owner.

GROUND RULES
1. Work ONLY inside /home/root509/stocks. Never touch another project; never install system-wide.
2. One agent at a time (L2). Before EVERY edit, confirm the file's modification time is unchanged
   since you read it. If it changed, STOP and report.
3. Snapshot before the first edit of each package:
   git update-ref refs/snapshots/before-<id> $(git stash create "before <id>")
4. Follow the order in section 1 unless the owner says otherwise. One package at a time.
5. Packages marked "design note first" (L8): write the note, then STOP for approval.
   B1 and anything touching money code also needs approval (L3).
6. Every behaviour change has a test that fails without it. Finish each package with bug
   re-insertion (L5).
7. After each package, run ALL suites and compare with the baseline in this document's header:
   API (cd apps/api && .venv/bin/python -m pytest -q), agent (cd provider-agent &&
   .venv/bin/python -m pytest -q), Python SDK (cd sdk/python && ../../apps/api/.venv/bin/python -m
   pytest -q tests), TypeScript SDK (cd sdk/typescript && npx vitest run && npx tsc --noEmit),
   guardian/witness (apps/api/.venv/bin/python -m pytest -q apps/guardian apps/witness),
   e2e (apps/api/.venv/bin/python -m pytest -q -rs tests/e2e), web (cd apps/web && npx vitest run
   && npm run typecheck && npx next lint && npm run build), contracts (cd contracts && npx hardhat test).
   Once B3 is done, also the Postgres suite.
8. Tick a checklist item (section 7) ONLY with its proof written next to it.
9. Never commit or push without the owner's instruction; if on main, branch first.
10. Report in the section 9 format after each package, then WAIT.

TRAPS ALREADY HIT IN THIS CODEBASE
- Async SQLAlchemy cannot lazy-load: query, don't touch relationship attributes on objects
  loaded without eager loading (see app/services/gpu_runtime.py).
- Raising HTTPException rolls back the request's writes; return a JSONResponse when the failure
  itself must be recorded.
- Never read a security claim from an unsigned field next to signed bytes.
- Mocks follow the vendor's documented API, never the code's assumptions (L4).
- Docs must match the code: state real status codes and real behaviour; never mark "done"
  what is not proven; use relative links, never file:// paths.
- Keep test scratch inside the project (.tools/) or pytest's tmp_path; no network in tests.
~~~

---

## 4. Track A (pointer)

Design, tests and the item-level checklist are in
[production-readiness-master-plan.md](production-readiness-master-plan.md) sections 4–5. This
document tracks only whether each package is complete (section 7, Track A).

---

## 5. Track B designs

### B1 Token withdrawals (money code; blocked on owner decisions)

**Gate.** The owner answers ADR-0004 section 5 (rate, direction, limits, gas, legal), and a
design note is approved (L3, L8). Mainnet stays off until the audit and legal review.

**Design (after approval).**
1. Withdrawal request: amount, destination = one of the user's **signature-verified**
   `LinkedWallet` addresses on that chain only. Idempotency key required.
2. Ledger: move the amount to a `withdrawal_pending` account in one double-entry transaction;
   per-transaction and daily caps from the approved decisions; minimum amount.
3. Payout worker: sends the ERC-20 transfer from a hot wallet whose key comes from a KMS/HSM
   (never a file); one in-flight transaction per wallet; waits for the agreed confirmations.
4. Success → `withdrawal_pending` → `paid_out`; revert, timeout or rejection → refunded to the
   user with a record. Every state change is in the ledger and audit log.
5. Reconciliation: extend `chain_reconciler.py` to compare chain transfers with ledger payouts;
   a mismatch pauses withdrawals automatically.
6. Kill switch: `WITHDRAWALS_ENABLED` (default off) plus an admin pause.

**Tests.** Local Hardhat chain end to end: request, cap, idempotent retry, success, revert →
refund, reorg below confirmations, reconciliation mismatch → pause, wrong-chain or unlinked address refused.

### B2 Streaming chat responses

**Design note first.**
1. OpenAI-compatible server-sent events: `Content-Type: text/event-stream`, one
   `data: {chat.completion.chunk}` per piece, final `data: [DONE]`.
2. Simulated models: stream the simulated text in chunks (same final text as non-streaming).
3. Real models (after A3): the agent sends token chunks with sequence numbers to a new
   machine-signed endpoint; the API relays them in order; gaps or reordering end the stream with an error.
4. Billing unchanged (metered seconds). The client disconnecting cancels the job and releases the
   unused hold.
5. Private tiers: streaming refused (`streaming_not_supported_for_private`) until an encrypted
   streaming design exists (U18).

**Tests.** Chunk format and order; `[DONE]`; text equals the non-streaming result; disconnect
cancels and refunds; private refused; agent stream endpoint rejects unsigned or out-of-order chunks.

### B3 CI and concurrency tests on Postgres

**Facts.** `apps/api/tests/conftest.py` hard-wires `sqlite+aiosqlite:///:memory:`; the CI API job
uses `sqlite+aiosqlite:///./ci.db`; there is no Postgres service in `.github/workflows/ci.yml`.

**Design.**
1. Conftest reads `TEST_DATABASE_URL` (default: today's SQLite), creating and dropping a fresh
   schema per test session on Postgres.
2. CI: an extra job with a `postgres:16` service runs `alembic upgrade head`, `alembic downgrade
   base`, `alembic upgrade head`, then the full API suite against Postgres.
3. Postgres-only concurrency tests (marker `postgres_only`, skipped on SQLite): two concurrent
   claims → one job; lease recovery with `skip_locked` never double-recovers; two concurrent
   transparency-log appends get distinct indices; concurrent ledger postings on one account stay
   balanced; concurrent key-release requests release once.

**Tests.** The concurrency tests above, each shown to fail with locking removed (bug re-insertion).

### B4 Load tests and capacity baseline

**Design.**
1. `tests/load/` with a pinned load tool (Locust, Python, fits the repo); scenarios: browse the
   marketplace, submit jobs, agent heartbeat + claim loop (signed requests), inference calls.
2. Run against the local stack (`docker-compose.yml`) on Postgres + Redis; never against
   production without approval.
3. Record p50/p95/p99 latency, error rate and throughput per scenario in
   `docs/operations/load-test-baseline.md` with the machine spec; write a short capacity plan
   (when to scale API, worker, database).
4. Opt-in CI job (manual trigger) that fails on a regression beyond an agreed threshold.

### B5 Per-job network allowlist

**Design note first.** (Also required by ADR-0006 U3.3 and U8.)
1. Job field `network_policy: {"egress": ["host:port", ...]}`, default none. Server validation:
   at most N entries; hostnames only; refuse IP literals, private and link-local ranges, cloud
   metadata (`169.254.169.254`), `localhost`; refuse for privacy tiers (L10).
2. Agent: the job runs on a per-job internal Docker network with no route out; a pinned egress
   proxy (by digest) on that network allows only the listed host:port (HTTPS via CONNECT / SNI);
   the container gets `HTTPS_PROXY`/`HTTP_PROXY`; everything else is dropped and logged.
3. Proxy and network are removed when the job ends; blocked attempts are reported in the job log.
4. Fix the `executor.py` docstring, which says an allowlist exists.

**Tests.** Server validation cases; agent argv (internal network, proxy, env); allowed host
reachable and blocked host refused in an opt-in Docker test; metadata address blocked; privacy tier refused; NVIDIA and AMD argv otherwise unchanged.

### B6 GPU thermal capping

**Design.**
1. Agent config: `gpu_temp_cap_c` (default 85), `gpu_temp_resume_c` (default 80),
   `gpu_temp_hard_stop_c` (default 95), optional `gpu_power_limit_w`.
2. Using the existing telemetry (NVML for NVIDIA, amdsmi for AMD): at or above the cap, the agent
   stops claiming new jobs and reports `availability: "throttled"`; it resumes below the resume
   temperature (hysteresis).
3. At or above the hard stop, the running job's container is stopped and reported failed with
   `thermal_shutdown`, so the customer is not charged and the job can fail over.
4. Server: `throttled` machines are not scheduled; the admin telemetry page shows it.
5. Power limit: set through NVML / amdsmi only when the agent has permission; otherwise log and
   continue (never fail enrolment for it).

**Tests.** Fake telemetry: cap stops claims; hysteresis; hard stop fails the job without charge;
scheduler skips `throttled`; missing readings (None) never trigger or block anything.

### B7 Website accessibility

**Design.**
1. Lint: enable `eslint-plugin-jsx-a11y` (recommended rules; pinned version) in the web lint.
2. Automated checks: axe-core (pinned) on the key pages (landing, sign-up, login, dashboard,
   new job, job page, marketplace, provider hardware) in vitest with jsdom, or Playwright if a
   real browser is needed. Zero serious or critical violations.
3. Fix what they find: alt text, labelled form inputs, keyboard focus order and visible focus,
   `aria-live` for form errors and toasts, colour contrast ≥ 4.5:1 (script that checks the
   design-token colour pairs).
4. A manual keyboard-only and screen-reader pass on the same pages, recorded in `qa2.md`.

---

## 6. Track C plan (ADR-0006)

Each package starts with a design note (L8) that turns the ADR section into interfaces, data
model, migrations, security and tests; the owner approves it before code. The checkpoints
(Ux.y) are ADR-0006's own, listed in section 7.

| Package | Depends on | Owner decisions needed before the design note |
|---|---|---|
| U1 Job Spec v2 + Kit Registry | A3 | Kit signing key custody |
| U2 Data plane | U1 | Storage provider (S3 / R2 / other), retention, size limits |
| U3 Agent runtime v2 | U1, U2, B5 | Cache size limits on hosts |
| U4 Fan-out and merge | U1, U2, U3 | — |
| U5 Kits wave 1 | U1–U4 | Which kits first; software licences (ADR-0006 §14) |
| U6 Verification plug-ins | U5 | Acceptance thresholds per kit |
| U7 Scheduler v2 | U1 | — |
| U8 BYOC | U3, B5 | Image scanning service; default isolation |
| U9 Pipelines | U4 | — |
| U10 Interactive sessions | U3 | Relay provider (hosts stay outbound-only) |
| U11 Services / endpoints | U7 | Autoscaling limits, pricing |
| U12 Kits waves 2–3 | U5 | Kit list |
| U13 Multi-GPU / multi-node | U7 | Which hosts may be "same datacenter" |
| U14 Windows VM tier | U3 | **Go / no-go** (optional) |
| U15 Pricing units | U4, U5 | Money code: approval (L3) |
| U16 Acceptable use and safety | — | **Legal sign-off required** |
| U17 Web experience | U5 | — |
| U18 Privacy across modes | U4, U10, ADR-0005 | — |
| U19 Observability | U3 | Dashboard tool |

---

## 7. Master checklist

Tick an item only with its proof written after it.

### Track A (complete = every item of that package ticked in production-readiness-master-plan.md)
- [ ] A1 Stake sync complete (P1.1–P1.11). *Proof: link to ticked items.*
- [ ] A2 Revocation, TDX module, NVIDIA RIM complete (P2.1–P2.7). *Proof: link.*
- [ ] A3 Workload images + real inference complete (P3.1–P3.8; P3.7–P3.8 need hardware). *Proof: link.*
- [ ] A4 Agent evidence complete (P4.1–P4.6; P4.6 needs hardware). *Proof: link.*
- [ ] A5 Sealed OS image complete (P5.1–P5.5; P5.5 needs hardware). *Proof: link.*
- [ ] A6 AMD in Sealed tiers complete (P6.0–P6.3; P6.0 owner decision, P6.3 hardware). *Proof: link.*

### B1 Token withdrawals
- [ ] B1.0 Owner answered ADR-0004 §5; design note approved. *Proof: recorded decisions + note.*
- [ ] B1.1 Destination limited to signature-verified linked wallets on the same chain. *Proof: tests.*
- [ ] B1.2 Ledger hold, caps, minimum, idempotency. *Proof: tests.*
- [ ] B1.3 Payout worker with KMS-held key, one in-flight transaction, confirmations. *Proof: local-chain test.*
- [ ] B1.4 Revert / timeout → refund with record. *Proof: tests.*
- [ ] B1.5 Reconciliation mismatch pauses withdrawals. *Proof: test.*
- [ ] B1.6 Off by default; admin pause works. *Proof: tests.*
- [ ] B1.7 Independent review of the money code. *Proof: review report.*

### B2 Streaming
- [ ] B2.0 Design note approved. *Proof: note.*
- [ ] B2.1 SSE chunk format and `[DONE]`; streamed text equals non-streamed. *Proof: tests.*
- [ ] B2.2 Client disconnect cancels the job and releases the unused hold. *Proof: test.*
- [ ] B2.3 Private tiers refuse streaming. *Proof: test.*
- [ ] B2.4 Real-token relay from the agent (after A3): signed, ordered, gap-checked. *Proof: tests.*

### B3 Postgres CI
- [ ] B3.1 Conftest honours `TEST_DATABASE_URL`; SQLite stays the default. *Proof: both runs.*
- [ ] B3.2 CI Postgres job: migrations up / down / up, full API suite. *Proof: CI run.*
- [ ] B3.3 Concurrency tests (claim, lease recovery, log append, ledger, key release) pass on Postgres and fail with locking removed. *Proof: tests + bug re-insertion.*

### B4 Load tests
- [ ] B4.1 Scenarios in `tests/load/` with a pinned tool. *Proof: files.*
- [ ] B4.2 Baseline recorded (p50/p95/p99, errors, throughput, machine spec). *Proof: load-test-baseline.md.*
- [ ] B4.3 Capacity plan written. *Proof: doc.*
- [ ] B4.4 Opt-in CI job with a regression threshold. *Proof: CI run.*

### B5 Network allowlist
- [ ] B5.0 Design note approved. *Proof: note.*
- [ ] B5.1 Server validation (limits, no IP literals / private / link-local / metadata / localhost; privacy tiers refused). *Proof: tests.*
- [ ] B5.2 Agent: internal network + pinned egress proxy; only listed hosts reachable; blocked attempts logged. *Proof: argv tests + opt-in Docker test.*
- [ ] B5.3 Default remains no network; NVIDIA/AMD argv otherwise unchanged. *Proof: snapshot tests.*
- [ ] B5.4 `executor.py` docstring matches behaviour. *Proof: diff.*

### B6 Thermal capping
- [ ] B6.1 Cap stops new claims and reports `throttled`; hysteresis resume. *Proof: tests.*
- [ ] B6.2 Hard stop fails the running job with `thermal_shutdown`, no charge. *Proof: test.*
- [ ] B6.3 Scheduler skips `throttled` machines. *Proof: test.*
- [ ] B6.4 Missing readings never trigger or block; power limit only when permitted. *Proof: tests.*

### B7 Accessibility
- [ ] B7.1 `eslint-plugin-jsx-a11y` enabled; lint clean. *Proof: lint output.*
- [ ] B7.2 axe checks on the key pages: zero serious/critical violations. *Proof: test output.*
- [ ] B7.3 Contrast check of design-token pairs ≥ 4.5:1. *Proof: script output.*
- [ ] B7.4 Manual keyboard and screen-reader pass recorded in `qa2.md`. *Proof: record.*

### Track C (ADR-0006 checkpoints; each package also needs its design note approved)
- [ ] U1.0 Design note approved · [ ] U1.1 v1 jobs unchanged · [ ] U1.2 invalid specs refused with field errors · [ ] U1.3 published Kit immutable · [ ] U1.4 Kit signature checked
- [ ] U2.0 Design note approved · [ ] U2.1 50 GB upload resumes · [ ] U2.2 corrupted part re-sent · [ ] U2.3 duplicate stored once · [ ] U2.4 BYO-bucket credentials scoped and expiring
- [ ] U3.0 Design note approved · [ ] U3.1 inputs read-only · [ ] U3.2 cache prevents re-download · [ ] U3.3 egress outside allowlist blocked and logged · [ ] U3.4 resume from checkpoint on another host
- [ ] U4.0 Design note approved · [ ] U4.1 500 frames fanned out and merged in order · [ ] U4.2 failed part re-runs alone, no double billing · [ ] U4.3 straggler copy, one result kept
- [ ] U5.0 Design note approved · [ ] U5.1 each Kit passes smoke tests on ≥ 3 GPU models · [ ] U5.2 web form from Kit schema · [ ] U5.3 quotes within ±20%
- [ ] U6.0 Design note approved · [ ] U6.1 perceptual check catches an altered frame · [ ] U6.2 PSNR/SSIM catches a bad encode · [ ] U6.3 method and result shown
- [ ] U7.0 Design note approved · [ ] U7.1 VRAM requirement respected · [ ] U7.2 cache affinity · [ ] U7.3 4-GPU on one host or wait · [ ] U7.4 existing scheduler tests green
- [ ] U8.0 Design note approved · [ ] U8.1 BYOC isolated by default · [ ] U8.2 known-bad images refused · [ ] U8.3 escape test suite fails to escape
- [ ] U9.0 Design note approved · [ ] U9.1 render → upscale → encode pipeline · [ ] U9.2 restart from the failed step
- [ ] U10.0 Design note approved · [ ] U10.1 works behind NAT, no open ports · [ ] U10.2 relay sees only ciphertext · [ ] U10.3 idle stops billing
- [ ] U11.0 Design note approved · [ ] U11.1 scales to a second host · [ ] U11.2 failover within 60 s · [ ] U11.3 billing reconciles
- [ ] U12.0 Kit list approved · [ ] U12.x each new Kit meets U5.1–U5.3
- [ ] U13.0 Design note approved · [ ] U13.1 4-GPU fine-tune on one host · [ ] U13.2 multi-node only on benchmarked same-datacenter hosts
- [ ] U14.0 Go / no-go decided · [ ] U14.1 Windows job in an isolated VM · [ ] U14.2 host cannot reach the VM except via the agent
- [ ] U15.0 Design note approved (money code, L3) · [ ] U15.1 unit quotes from GPU-seconds, cap never exceeded · [ ] U15.2 egress and storage shown separately
- [ ] U16.0 Design note approved · [ ] U16.1 AUP accepted at sign-up · [ ] U16.2 mining detected and stopped · [ ] U16.3 counsel sign-off recorded
- [ ] U17.0 Design note approved · [ ] U17.1 non-technical user renders without docs (5 users) · [ ] U17.2 mobile monitoring and download
- [ ] U18.0 Design note approved · [ ] U18.1 private fan-out: per-part and job receipts verify · [ ] U18.2 private session stream unreadable by relay and host
- [ ] U19.0 Design note approved · [ ] U19.1 live per-job utilisation, progress and cost · [ ] U19.2 platform dashboards

### Sign-off (per package and at the end)
- [ ] S1 Money code changed only inside B1 / U15 with approval (L3). *Proof: `git diff --stat`.*
- [ ] S2 All suites green, counts compared with the baseline (plus Postgres once B3 is done). *Proof: outputs.*
- [ ] S3 Bug re-insertion run for every new check; restores verified by checksum. *Proof: recorded run.*
- [ ] S4 External interfaces recorded with doc URL + date (L4). *Proof: list.*
- [ ] S5 No secrets anywhere (L7). *Proof: scan.*
- [ ] S6 Docs, ADR statuses and stale checklists updated to match the code. *Proof: diff.*
- [ ] S7 Independent code review on the diff; findings fixed or recorded. *Proof: report.*
- [ ] S8 Owner approval before merge.

---

## 8. Owner decisions this plan waits on

| Decision | Blocks |
|---|---|
| D5 change: AMD GPUs in the Sealed tiers | A6 |
| ADR-0004 §5: rate, direction, limits, gas, legal | B1 |
| Storage provider and limits | U2 |
| Kit signing key custody; first kits and their licences | U1, U5 |
| Relay provider for sessions | U10 |
| Windows VM tier go / no-go | U14 |
| Pricing units design (money code) | U15 |
| Acceptable-use policy and counsel sign-off | U16 |

---

## 9. Report format (after each package)

| Section | Content |
|---|---|
| Files changed | One line each on why |
| Tests added | Names, and the failure each one pins |
| Suites | Before / after counts for every suite |
| Bug re-insertion | Each check removed → test that failed → restore verified |
| Checklist | Items ticked with proof; items not done and why |
| External docs | URL + date for every interface used (L4) |
| Decisions | Any owner decision needed before the next package |
| Risks | What is still uncertain |
