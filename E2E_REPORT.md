# ComputeCoin — End-to-End Run Report (customer + provider + admin)

Date: 2026-10-24. Branch `main`, working tree uncommitted (changes listed below).

## How it was run

A throwaway Postgres 16 cluster, the FastAPI API (local artifact store, no Redis), the Next.js
web app, and the **real provider agent** (`computecoin_agent`). Everything below was driven as a
user would drive it: through a real Chrome browser against the UI where a UI exists, and through
the real agent for the provider side. No mocks.

Not covered (needs infrastructure that is not on this machine): Docker/`make up`, MinIO/S3,
Redis-backed rate limiting, a real GPU (`RUN_GPU_TESTS`), Stripe, and the testnet chain tests
(27 API tests are opt-in skips for this reason).

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

## Regression tests added

- `tests/test_phase7_admin.py::test_admin_lists_all_machines_without_being_a_provider`
- `tests/test_pg_concurrency_live.py::test_concurrent_first_billing_calls_create_one_account`
- `tests/test_pg_concurrency_live.py::test_concurrent_first_provider_earnings_calls_create_one_account`
- `tests/test_storage_s3_live.py` (opt-in, `RUN_S3_INTEGRATION=1`; 5 tests)
- `tests/test_email_verification_gate.py::test_resend_verification_*` (2 tests)

The two concurrency tests need real Postgres (SQLite cannot show this race). I confirmed both
**fail with the fix reverted and pass with it**.

## Test results

- API: 433 passed, 34 skipped (opt-in: chain, GPU, live Postgres, live S3), 0 failed.
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
