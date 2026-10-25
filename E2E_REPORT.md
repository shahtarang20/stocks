# ComputeCoin — End-to-End Run Report (customer + provider + admin)

Date: 2026-10-24. Branch `main`, working tree uncommitted (changes listed below).

## How it was run

A throwaway Postgres 16 cluster, the FastAPI API (local artifact store, no Redis), the Next.js
web app, and the **real provider agent** (`computecoin_agent`). Everything below was driven as a
user would drive it: through a real Chrome browser against the UI where a UI exists, and through
the real agent for the provider side. No mocks.

Not covered (needs infrastructure that is not on this machine): Docker/`make up`, a real GPU
(`RUN_GPU_TESTS`) and live Stripe. Redis, S3 and SMTP were run locally from Round 2; the token and
chain code was run against a local Hardhat chain (25 contract tests and 51 API chain tests pass).

## Journeys exercised

| Role | Journey | Result |
|---|---|---|
| Provider | Register in UI → become provider → mint enrollment token in UI → real agent enrolls with it → machine on Hardware page → heartbeat → agent claims and runs a job → earnings accrue → payout request in UI → overdraw payout rejected (409) | Pass |
| Customer | Register/login in UI → create project → create API key → key authenticates `/v1/chat/completions` → revoke → key refused → submit job from the form → completes → simulated artifact → invoice → usage → buy credits → open dispute | Pass |
| Admin | Login → every admin page loads → suspend customer in UI → customer cannot log in → reinstate | Pass |
| Auth | Wrong password message, anonymous redirect, non-admin blocked from admin API | Pass |
| Platform | Lease expiry recovery, 12-way concurrent reservation (one wins), double-submit race (one wins, one 402), ledger balanced before/after refunds | Pass |

Pages swept in the browser: 13 customer, 5 provider, 13 admin, 10 public — all render with no
error text, `NaN`/`undefined`, or JS exceptions after the fixes below.

## Bugs found and fixed

| # | Severity | Bug | Fix |
|---|---|---|---|
| 1 | **High** | **Projects and API keys were broken in the UI.** The web client called `/v1/projects…` for create, delete and all API-key operations; the API serves `/projects…`. "New project", "Create key", list and revoke all returned 404. (The e2e scripts hit the API directly so never saw it.) | `apps/web/src/lib/api.ts` — paths corrected. I then diffed every endpoint the client calls against the API's OpenAPI spec: no other mismatches. |
| 2 | **High** | **First visit to Billing 500'd intermittently for new users.** `get_or_create_customer_account` was check-then-insert; the page fires balance + ledger together, so both tried to create the account and the loser hit `uq_billing_accounts_owner_type`. Reproduced: 7 of 40 concurrent requests failed on the old code, 0 of 160 after. | `services/ledger.py` — take the per-customer advisory lock, re-check, then insert. |
| 3 | **High** | **Same race on the provider side** (`get_provider_account`; earnings and payouts pages load concurrently for a new provider). | Same pattern, keyed per provider. |
| 4 | Medium | **Admin telemetry machine picker always 403'd.** It listed machines via `/provider/machines`, which is scoped to the caller's own provider profile, so an administrator could never select a machine. | New `GET /v1/admin/machines` (administrator-only) in `routers/admin.py`; web client and telemetry page use it. |
| 5 | Medium | Revoked API keys looked identical to live ones and still offered a Revoke button. | Show a "Revoked" badge, hide the button (`revoked_at` was already returned by the API). |
| 6 | Low | React hydration errors: `Skeleton` (a `div`) rendered inside `<p>` on the provider dashboard and admin overview while loading. | Those wrappers are now `div`s. |
| 7 | Low | `/favicon.ico` 404 on every page. | Added `src/app/icon.svg`. |

## Round 2: production-like stack (Postgres + Redis + S3 + SMTP)

Redis (built from source), an S3-compatible server (moto) and a local SMTP sink were run as
user-space processes on localhost only. The API ran against all of them.

| # | Severity | Finding | Fix |
|---|---|---|---|
| 8 | **Blocker** | **Object storage did not work in any deployed config.** `S3Store` was a stub that raised on every call, yet `docker-compose.yml`, `docker-compose.prod.yml` and `.env.example` all set `OBJECT_STORAGE_ENDPOINT`. A deployed stack could not store one job output, dataset or checkpoint. Local-filesystem dev hid it. | Real boto3 `S3Store` (path-style, retries, auto-creates the bucket, rejects traversal keys, `FileNotFoundError` on missing keys like the local store); `boto3` added to `pyproject.toml`; access key / secret / region settings added. Verified: all e2e journeys pass with artifacts landing in the bucket. |
| 9 | **Blocker** | **Emailed links led to 404.** The API emails `/verify-email?token=` and `/reset-password?token=`, but the web app had neither page, and no forgot-password page or link. Nobody could finish signup or recover a password through the product. | New `/verify-email`, `/forgot-password`, `/reset-password` pages and a "Forgot password?" link on login. Tested in a browser against real SMTP delivery: verify, single-use link, mismatched passwords, reset, old password rejected, new password works (13/13). |
| 10 | High | **An unverified user had no way to get a new link** (the first expires in 24h) and was locked out of billing and jobs, seeing only a bare 403. | `POST /auth/resend-verification` (session required, 3 per hour per account, no account probing) and a console banner with a Resend button. Tests added; browser flow passes. |
| 11 | Medium | `.env.example` used names the app never reads (`EMAIL_PROVIDER_CONFIGURATION`, `RATE_LIMIT_CONFIGURATION`), and did not mention that production refuses to start unless `SETTLEMENT_ENABLED=false` or a signer key is set. Following it would leave email on console. | Replaced with the real settings and a production block. |

Verified on the stack: Redis-backed rate limiting (login blocked with 429 after 5 tries),
production mode refuses dev secrets and starts with strong ones, `/docs` is off in production,
cookies are `Secure`/`HttpOnly`, register no longer returns the verification token, and phases
2 to 4 plus both journey scripts pass. Load: 400 public requests at 233 req/s, 0% errors;
1,200 authenticated requests from 30 concurrent clients at about 200 req/s, p95 220 ms,
0 errors, 0 server errors (laptop numbers, not staging capacity).

## Round 3: every control, every role, plus fuzzing

About 360 control-level browser checks (`tests/qa/`, full list in `docs/QA_CHECKLIST.md`) and
two fuzzers (about 15,000 hostile requests across every endpoint). All 356 browser checks pass on
the production build from a fresh seed. The fuzzers found 6 endpoint groups returning 500; all fixed,
and a re-run returns no 5xx (the only 503 is chain reconciliation correctly reporting that no chain
is configured).

| # | Severity | Finding | Fix |
|---|---|---|---|
| 12 | **High** | **Money inputs crashed or misbehaved.** `NaN`, `Infinity`, `1e30`, `0.001`, garbage and booleans returned 500 from credit purchase, payouts, disputes and Stripe Connect; `1.999` was silently rounded up to 2.00; a 999,999,999,999 top-up went straight through (no ceiling). | One strict `parse_money` (finite, at most 2 decimals, at most 1,000,000) used by every money schema. |
| 13 | **High** | **Duplicate disputes.** Nothing stopped unlimited disputes on one job (each adds evidence and an admin task). The UI defined a "dispute exists" message the backend never produced. | Job row lock plus a 409 `dispute_exists` while one is open. |
| 14 | **High** | **Admin Disputes always looked empty.** It called the customer's own-disputes endpoint instead of the admin one. | Now uses `/v1/admin/disputes`. |
| 15 | **High** | **Artifact downloads went to the wrong host.** Signed links are API-relative, so the browser opened them on the web origin (404 anywhere web and API differ, i.e. production). | `apiAbsoluteUrl()` for artifact and invoice links. |
| 16 | **High** | **Invoice Download did nothing.** The endpoint returns the invoice, not a link, and the page only handled a link. | Saves the invoice as a JSON file. |
| 17 | Medium | Unknown `machine_id` on job submit was a foreign-key 500 (shown in browsers as a CORS error). | 404 `machine_not_found` before the insert. |
| 18 | Medium | Negative `limit`, or huge `offset`/`page`, 500'd about a dozen list endpoints. | Range limits on every pagination parameter. |
| 19 | Medium | A NUL character in any text field (project, org, token label) was a 500 on Postgres. | One handler returns 422 `invalid_characters`. |
| 20 | Medium | Enrolling a second machine with an existing hostname was a bare 500; a deregistered host could never re-enroll. | 409 `machine_hostname_already_enrolled` (token not consumed); deregistered hostnames are released. |
| 21 | Medium | Usage totals read 0.00 and durations read a dash even with metered jobs (UI read fields the API does not send). Disputed jobs read "not charged" despite a charge. | UI reads the real fields; cost shows whenever `final_cost` exists. |
| 22 | Medium | Project Description was collected, sent, and discarded (no column). | Migration 0014, model, schemas, and the list now shows it. |
| 23 | Medium | Login ignored `?next=` (deep link then login always landed on the dashboard). | Honoured, same-site paths only (no open redirect). |
| 24 | Low | Raw codes like `isolation_tier_not_supported_by_machine` shown to users. | Plain-English messages for every job and billing error code, and a readable fallback. |
| 25 | Low | Region filter was exact and case-sensitive ("US-East" matched nothing) while the model filter was a substring. | Case-insensitive substring. |
| 26 | Low | Mobile (390px) overflow on API keys (long project name) and Admin vendors. | Fixed. |
| 27 | Low | False or stale copy: "Audited smart contracts" (not audited), "tokens stored in your browser", "job submission is stubbed". | Corrected. |

Regression tests added for 12 to 20, 22, 23 and 25, including pagination, amounts, disputes,
hostnames, NUL characters, projects and region.

### Product gaps found, and what was done about them

| Gap | Outcome |
|---|---|
| Account deletion was a demo no-op; profile editing a disabled placeholder | **Built.** Display name; password change (ends every session); account deletion that anonymises the user, revokes sessions, API keys, webhooks, enrollment tokens and machines, and keeps jobs, invoices and the ledger intact and balanced. Needs the password, an explicit "I give up my remaining credits" tick, and is refused while jobs, held credits, open disputes or pending payouts exist. Admins cannot self-delete or reinstate a deleted account. The email is freed for a fresh sign-up. |
| Customer dashboard and provider dashboard charts, and the public `/marketplace`, showed demo data | **Replaced with real data.** Customer dashboard: credits, job count, spend and compute hours from the API, an empty state for new users; provider dashboard: recent earnings from the ledger; `/marketplace` forwards to the live listing; the status page is now a live API/database/cache health check. The fake "utilization" tile was removed because nothing measures it. |
| A rejected job submission still appeared as a failed job in the customer's list | **Fixed.** Kept for the audit trail and visible to admins, hidden from the customer's own list. |

Also done after a live-site test: **sign-up now asks "Use GPUs" or "Host my GPU"** (hosts are registered as providers and sent to onboarding), and **provider pages show a friendly "You are not a host yet / Become a host" prompt** instead of a red error for accounts that have not become hosts. An account can be both.

### Round 4: wallet and token balance (phase 1 of the token work)

| What | Outcome |
|---|---|
| Wallet linking | `POST /v1/wallet/link`: a user proves control of an address by signing a server-issued message (EIP-191). The message names the account, address, chain and time and carries an HMAC tag, so a signature made for one account, one address, an edited message, an expired message or a different key is refused. One address belongs to one account per chain; one account has one wallet per chain. Migration 0016. |
| Token balance | `GET /v1/wallet` returns credits and the linked wallet's on-chain COMPUTE balance, read by a JSON-RPC `balanceOf` call. Verified against the real deployed token (matches an independently encoded call; fresh address 0; no contract is an explicit error). A chain outage shows "unavailable" and never breaks the page. |
| Wallet page | `/dashboard/wallet` (also in the host navigation): credits (simulated) and the on-chain wallet side by side, connect and link with a browser wallet (no wallet library bundled), unlink, explorer link, and an explicit "test network, no monetary value" notice. |
| Found while doing it | `scripts/deploy-testnet.mjs` failed on the second contract with "nonce has already been used" (fixed with a NonceManager; public testnets are exposed to the same race). A testnet key in `contracts/.env` silently broke local-chain deployments, so testnet settings now live in a separate `contracts/testnet.env` loaded only on request (runbook updated). |
| Not built, on purpose | Moving value between credits and tokens. [ADR-0004](docs/architecture/ADR-0004-token-and-ledger.md) lays out the options, the invariant a bridge must keep, and the five owner decisions needed first (rate, direction, limits, gas, legal). |
| Public testnet | BNB testnet RPC is reachable and a throwaway deployer wallet exists, but **it needs test BNB from the faucet before the real deploy can run**; `scripts/testnet-balance.mjs` reports when it is ready. |

### Round 5: governance for the token, and token deposits (steps 2 and 3 of the launch list)

| What | Outcome |
|---|---|
| **Governance** | New `ComputeTimelock` and `ComputeVesting` (thin, unmodified OpenZeppelin wrappers; the token and settlement logic is untouched, so there is nothing new to audit in them). `scripts/launch.mjs` takes a written allocation plan (basis points that must total 10,000, real addresses, sane cliffs), deploys, splits the supply exactly, locks vested shares, and hands ownership to a multisig and a timelock. The deployer ends with zero tokens and no rights. A placeholder plan is refused on any mainnet chain id, and an approved one still needs `I_UNDERSTAND_MAINNET=yes`. |
| **Proof** | `scripts/launch-rehearsal.mjs` runs 41 self-checks on a real local chain: exact split, vesting before/after the cliff and at the end, timelock delay enforced (strangers refused, early execution refused, execution after the delay works), no admin backdoor. Timing is deterministic (10 of 10 repeat runs pass). |
| **Deposits (stage 1 of ADR-0004)** | COMPUTE sent to the platform address from a **linked** wallet is credited to the ledger. Each transfer once (unique on chain, tx hash, log index), only after N confirmations, in integers rounded down to the cent, as a balanced ledger entry. Unlinked senders are `unclaimed` (credit nothing); out-of-range or sub-cent amounts are `rejected`. Reconciliation compares the chain with the ledger. Admin page "Token deposits"; the worker scans every sweep. **Off unless the rate and address are configured.** |
| **Tested for real** | Fake-chain unit tests (17, including rounding, confirmations, idempotency, deleted accounts, tampered rows), a real-chain test with a real transfer and a **real chain reorganisation** (a deposit reorganised away before confirming is never credited), and a browser test of the whole flow. Mutation checks confirm the tests fail when the rounding, confirmation or link rules are broken. |
| **Bug found by running on Postgres** | The ledger's reference column is 64 characters; a transaction hash plus log index is longer. SQLite never complains, so it passed every unit test and crashed the first real scan. Fixed (reference the deposit row), with a test that checks column lengths. |
| **Not done** | Withdrawals (credits back to tokens), the public-testnet deployment (still needs test coins), the audit and the legal review. The exchange rate, limits and who pays gas are owner decisions; nothing is switched on. |

Still open (not bugs, decide before launch): the admin audit log shows actors as truncated ids;
the admin Providers page lists only machines that are online; the ledger shows only recent
entries without pagination controls; the admin Configuration and Security pages are static
(they say so).

## Regression tests added

- `tests/test_phase7_admin.py::test_admin_lists_all_machines_without_being_a_provider`
- `tests/test_pg_concurrency_live.py::test_concurrent_first_billing_calls_create_one_account`
- `tests/test_pg_concurrency_live.py::test_concurrent_first_provider_earnings_calls_create_one_account`
- `tests/test_storage_s3_live.py` (opt-in, `RUN_S3_INTEGRATION=1`; 5 tests)
- `tests/test_email_verification_gate.py::test_resend_verification_*` (2 tests)
- `tests/test_account_profile_deletion.py` (9 tests: profile, password change, every deletion rule, anonymisation, provider machines, ledger stays balanced, admin guards)
- `tests/test_phase4_jobs.py::test_rejected_submissions_are_hidden_from_the_customers_job_list`

The two concurrency tests need real Postgres (SQLite cannot show this race). I confirmed both
**fail with the fix reverted and pass with it**.

## Test results

- API: 509 passed, 40 skipped (opt-in: chain, GPU, live Postgres, live S3), 0 failed. The opt-in live suites pass when switched on: Postgres/S3 9 of 9, local-chain 18 of 18 (ERC-20 balance read, deposits credited once, a real chain reorganisation).
- Browser QA (`tests/qa/`): 419 of 419 control-level checks pass on the production build from a fresh seed (customer 134, provider 60, admin 71, public 73, mobile 45, wallet 20, token deposits 16). `tests/qa/stack.sh` rebuilds the whole local stack, including a real local chain, and reruns everything.
- Fuzzing: about 15,200 hostile requests (POST/PUT/PATCH bodies and GET path/query parameters) across every endpoint, including the new profile, password and deletion endpoints; zero 5xx apart from the intentional 503 for an unconfigured chain.
- Provider agent: 66 passed, 1 skipped (needs a GPU).
- Web: `tsc` clean, `next lint` clean, production build passes.
- E2E scripts on a clean DB: full journey, deep journey (45/45), phases 2, 3, 4, 5 all pass.
- Live-PG concurrency suite: 4/4.

## Things that are not bugs but worth knowing

- **`phase5_billing_e2e.py` is environment-sensitive.** It assumes a 2.00 signup grant; the dev default is 1000.00 (`BILLING_SIGNUP_GRANT_MINOR`), so it reports 7 false failures unless that is set to `200`. It passes with the setting. Worth making the script set it itself or read the value.
- **`phase5` also hardcodes** user `ccuser` and database `cc_e2e` for its psql helper.
- `/provider/onboarding` deliberately probes `/provider/enrollment-tokens`, so a not-yet-provider sees a 403 in the browser console. This is by design, not a failure.
- Anonymous visits log 401s for `/auth/me` and `/auth/refresh` in the console. Expected.
- `apps/web/.env.local` (untracked) pins `NEXT_PUBLIC_API_BASE_URL` to port 8010, not the documented 8000. Anyone running the API on 8000 will see "Unable to reach the ComputeCoin API" on register. Local setup only, not a repo defect.
- Dev default `ENVIRONMENT=development` exposes verification tokens in the register response; the config guard refuses non-dev environments with insecure defaults.
