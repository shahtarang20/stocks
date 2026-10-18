# ComputeCoin — QA & Full-Stack Debugging Masterclass

**Document Version:** 2.0 — Production Audit Edition  
**Last Audit Date:** 2026-10-18  
**Audience:** QA Engineers, Full-Stack Developers, DevOps, Security Auditors  
**Scope:** Complete end-to-end testing methodology and bug resolution for a distributed GPU compute marketplace  

---

## Table of Contents

1. [Phase 0 — Pre-Flight Audit Checklist](#phase-0--pre-flight-audit-checklist)
2. [Phase 1 — Static Analysis & Code Quality](#phase-1--static-analysis--code-quality)
3. [Phase 2 — Unit Test Suite Execution](#phase-2--unit-test-suite-execution)
4. [Phase 3 — Integration Test Execution](#phase-3--integration-test-execution)
5. [Phase 4 — Security & Penetration Testing](#phase-4--security--penetration-testing)
6. [Phase 5 — Performance & Stress Testing](#phase-5--performance--stress-testing)
7. [Phase 6 — Live Production E2E Testing](#phase-6--live-production-e2e-testing)
8. [Phase 7 — Frontend Visual & Accessibility Audit](#phase-7--frontend-visual--accessibility-audit)
9. [Phase 8 — Smart Contract Audit](#phase-8--smart-contract-audit)
10. [Phase 9 — Infrastructure & Deployment Audit](#phase-9--infrastructure--deployment-audit)
11. [Phase 10 — Data Integrity & Migration Audit](#phase-10--data-integrity--migration-audit)
12. [Appendix A — Bugs Found & Fixed (This Audit)](#appendix-a--bugs-found--fixed-this-audit)
13. [Appendix B — Bug Resolution Methodology](#appendix-b--bug-resolution-methodology)
14. [Appendix C — Full Test Execution Commands](#appendix-c--full-test-execution-commands)

---

# Phase 0 — Pre-Flight Audit Checklist

Before touching a single line of code, the senior QA verifies the audit environment:

### 0.1 Repository Health
```bash
# Verify clean working tree (no uncommitted changes masking real state)
git status -s
git log --oneline -5

# Verify branch is up-to-date with remote
git fetch origin && git diff HEAD origin/main --stat
```

### 0.2 Dependency Lock Verification
```bash
# Backend — ensure lockfile matches installed packages
cd apps/api && .venv/bin/pip freeze | diff - <(pip freeze) || echo "DRIFT DETECTED"

# Frontend — verify node_modules matches lockfile
cd apps/web && npm ci --dry-run 2>&1 | grep -i "warn\|error"

# Smart contracts — verify Solidity compiler version pinned
cd contracts && cat hardhat.config.js | grep "solidity"

# Provider agent — verify isolated venv
cd provider-agent && .venv/bin/pip check
```

### 0.3 Environment Variable Audit
```bash
# Check for secrets committed to git (CRITICAL)
git log --all --diff-filter=A -- '*.env' '*.key' '*.pem' '*.secret'

# Verify .env.example covers all required variables
diff <(grep -oP '^[A-Z_]+' .env.example | sort) \
     <(grep -oP 'settings\.\K[A-Z_]+' apps/api/app/core/config.py | sort)

# Verify production config rejects dev defaults
grep "_reject_insecure_defaults_in_prod" apps/api/app/core/config.py
```

### 0.4 Architecture Components Inventory
Before testing, catalog every component in the system:

| Layer | Component | Technology | Location |
|-------|-----------|------------|----------|
| **API** | REST Backend | FastAPI + Uvicorn + Alembic | `apps/api/` |
| **Web** | Client Dashboard | Next.js 15 (App Router) | `apps/web/` |
| **Agent** | Provider Daemon | Python CLI + HMAC heartbeat | `provider-agent/` |
| **Contracts** | On-chain Escrow | Solidity + Hardhat | `contracts/` |
| **Database** | Persistent State | PostgreSQL (Supabase) | Alembic migrations |
| **Cache** | Rate Limit + Sessions | Redis (Upstash) | `REDIS_URL` |
| **Email** | Transactional | Resend / SMTP / Console | `services/email.py` |
| **Payments** | Billing | Stripe / Simulated | `services/stripe_payment.py` |
| **Storage** | Artifact Blobs | Local FS / S3 | `services/storage.py` |
| **Monitoring** | Error Tracking | Sentry (optional) | `main.py` |

---

# Phase 1 — Static Analysis & Code Quality

### 1.1 Python Linting & Type Checking
```bash
# Run ruff for fast Python linting
cd apps/api && .venv/bin/ruff check app/ tests/ --show-fixes

# Run mypy for type safety (if configured)
cd apps/api && .venv/bin/mypy app/ --ignore-missing-imports

# Provider agent
cd provider-agent && .venv/bin/ruff check computecoin_agent/ tests/
```

**What to look for:**
- `F841` — unused variables (potential logic bugs where a return value is discarded)
- `E711` — `x == None` instead of `x is None` (behavior differs in SQLAlchemy)
- `B006` — mutable default arguments (silent shared-state bugs across requests)
- `S101` — `assert` in production code (stripped by `python -O`)
- Type mismatches in `Decimal` vs `float` arithmetic (billing precision bugs)

### 1.2 Frontend TypeScript Checks
```bash
cd apps/web && npx tsc --noEmit   # Pure type-check, no build artifacts
```

**What to look for:**
- `any` type annotations (hides runtime crashes)
- Missing `null` checks on API responses
- Unhandled `Promise` rejections in `useEffect` hooks
- Hardcoded API URLs instead of `NEXT_PUBLIC_API_BASE_URL`

### 1.3 SQL Migration Chain Integrity
```bash
# Verify the migration chain is linear (no forks)
cd apps/api && .venv/bin/alembic history --verbose | head -30

# Verify head matches latest migration
.venv/bin/alembic heads  # Must show exactly ONE head

# Verify downgrade path exists for every upgrade
for m in $(ls alembic/versions/*.py | sort); do
  grep "def downgrade" "$m" > /dev/null || echo "MISSING DOWNGRADE: $m"
done
```

### 1.4 Solidity Static Analysis
```bash
cd contracts && npx hardhat compile  # Verify all contracts compile
# Run slither (if installed) for vulnerability detection
npx slither . --exclude-optimization 2>&1 | grep -E "(High|Medium|Info)"
```

---

# Phase 2 — Unit Test Suite Execution

### 2.1 Backend API Tests (166 tests across 25 files)

```bash
cd apps/api && .venv/bin/python -m pytest tests/ -v --tb=long -x
```

**Test Coverage Map:**

| Test File | Phase | Tests | Critical Invariants Verified |
|-----------|-------|-------|------------------------------|
| `test_auth_flow.py` | 1 | 3 | Registration, login, JWT lifecycle |
| `test_config_guard.py` | 1 | 2 | Dev secret rejection in production boot |
| `test_cookie_auth_csrf.py` | 1 | 5 | HttpOnly cookies, CSRF double-submit, SameSite |
| `test_email_service.py` | 1 | 4 | Console/Resend dispatch, missing API key handling |
| `test_health.py` | 1 | 1 | `/health` returns 200 |
| `test_login_security.py` | 1 | 2 | Brute-force rate limiting, generic error messages |
| `test_password_reset.py` | 1 | 3 | Token generation, expiry, one-time-use |
| `test_provider_phase2.py` | 2 | 14 | Enrollment tokens, HMAC heartbeats, stale detection |
| `test_phase3_marketplace.py` | 3 | 14 | Pricing math, quote itemization, capacity locking |
| `test_phase4_fsm.py` | 4 | 4 | Job state machine transitions, terminal states |
| `test_phase4_jobs.py` | 4 | 23 | Claim, artifacts, lease recovery, isolation |
| `test_phase5_billing.py` | 5 | 16 | Double-entry ledger, no negative balance, refunds |
| `test_phase6_inference.py` | 6 | 14 | Chat completions, embeddings, rate limiting |
| `test_phase7_admin.py` | 7 | 6 | User suspension, audit logs, platform stats |
| `test_phase8_isolation.py` | 8 | 4 | gVisor/MicroVM/Confidential tiers, attestation |
| `test_phase9_telemetry.py` | 9 | 5 | Time-series ingestion, threshold alerts |
| `test_phase10_settlement.py` | 10 | 8 | Escrow lifecycle, voucher signatures, reconciliation |
| `test_project_isolation.py` | 1 | 2 | Cross-tenant project isolation |
| `test_rbac.py` | 1 | 3 | Role-based access control enforcement |
| `test_refresh_rotation.py` | 1 | 2 | Refresh token rotation, chain revocation |
| `test_stripe_billing.py` | 5 | 12 | Webhook signature validation, idempotency, refunds |
| `test_api_keys.py` | 1 | 3 | API key CRUD, scoping, revocation |

**What the QA Tester Checks:**
1. **Every `assert` has a diagnostic message** — silent failures are unacceptable
2. **Tests are truly isolated** — each test creates its own user/machine/job, no shared state
3. **Async tests properly await** — missing `await` in pytest-asyncio is a silent skip
4. **Mocks restore original state** — `settings.EMAIL_PROVIDER` must be reset in `finally` blocks
5. **Edge cases for financial math** — `Decimal("0.005")` rounding with `ROUND_HALF_UP`

### 2.2 Provider Agent Tests (67 tests)

```bash
cd provider-agent && .venv/bin/python -m pytest tests/ -v --tb=long
```

**Critical Checks:**
- HMAC-SHA256 signature verification round-trips correctly
- Tampered payloads are rejected (bit-flip detection)
- Enrollment token single-use enforcement
- Docker executor refuses unpinned image tags (supply chain attack prevention)
- Simulated executor never claims "verified" hardware (honesty invariant)

### 2.3 Smart Contract Tests (12 tests)

```bash
cd contracts && npx hardhat test
```

**Critical Checks:**
- Token pause circuit breaker blocks settlements but allows refunds
- Double-lock on the same job escrow reverts
- Invalid EIP-712 signatures are rejected
- Fee cannot exceed cost (prevents treasury drain)
- Refund works after deadline expiry

---

# Phase 3 — Integration Test Execution

### 3.1 End-to-End Provider Lifecycle (Live API)
```bash
COMPUTECOIN_CONFIG_DIR=$(mktemp -d) \
CC_API_BASE=https://stocks-production-76fe.up.railway.app \
provider-agent/.venv/bin/python tests/end-to-end/phase2_provider_e2e.py
```

**Verified Flow:**
1. `POST /auth/register` → 201 Created
2. `POST /auth/verify-email` → 200 (or graceful skip in production)
3. `POST /auth/login` → 200 + access_token
4. `POST /provider/register` → 200
5. `POST /provider/enrollment-tokens` → 201 + `enr_` prefix token
6. Agent `enroll_remote()` → machine_id + `msk_` prefix secret
7. Config file persisted with `0600` permissions
8. Token is single-use (second attempt → `EnrollmentError`)
9. Machine visible in `GET /provider/machines`
10. `verification_status == "detected"` (never "verified" without benchmark)
11. Machine is `offline` before first heartbeat
12. Signed heartbeat → 200, machine transitions to `online`
13. Tampered heartbeat → 401 (HMAC rejection)
14. Cross-account isolation → 403

### 3.2 Concurrency Stress Test (Atomic Capacity Invariant)
```bash
COMPUTECOIN_CONFIG_DIR=$(mktemp -d) \
CC_CONCURRENCY=12 \
CC_API_BASE=https://stocks-production-76fe.up.railway.app \
provider-agent/.venv/bin/python tests/end-to-end/phase3_concurrency_e2e.py
```

**The One Rule:** A machine with `max_concurrent_jobs=1` MUST produce:
- **Exactly 1** reservation at HTTP 201
- **N-1** rejections at HTTP 409 (`no_capacity`)
- **Zero** HTTP 500s (no torn state, no double-booking)

### 3.3 Full Job Execution Lifecycle
```bash
COMPUTECOIN_CONFIG_DIR=$(mktemp -d) \
CC_API_BASE=https://stocks-production-76fe.up.railway.app \
provider-agent/.venv/bin/python tests/end-to-end/phase4_execution_e2e.py
```

**Verified Flow:**
1. Provider enrolls machine, brings online
2. Customer submits `text-generation` job → `reserved`
3. Idempotency: same `Idempotency-Key` → same job_id
4. Agent claims job → `running`
5. Agent executes (simulated) → uploads artifact → posts result
6. Job transitions to `completed`
7. `execution_mode == "simulated"` (honesty stamp — never claims GPU hardware)
8. Verification recorded as `valid`
9. FSM event history shows: `reserved → running → verifying → completed`
10. Logs appended and retrievable
11. Batch inference variant tested separately
12. Cancel flow: capacity freed, credits returned

### 3.4 Billing Double-Entry Ledger E2E
```bash
COMPUTECOIN_CONFIG_DIR=$(mktemp -d) \
CC_API_BASE=https://stocks-production-76fe.up.railway.app \
provider-agent/.venv/bin/python tests/end-to-end/phase5_billing_e2e.py
```

**Verified Invariants:**
- `SUM(debits) == SUM(credits)` after every transaction (double-entry conservation)
- Balance never goes negative (PostgreSQL `CHECK (balance_minor >= 0)`)
- Failed jobs are not charged
- Cancelled jobs are not charged
- Refunds reverse charges up to cumulative cap
- Provider payouts create symmetric debit/credit entries
- Stripe webhook idempotency key prevents phantom credits

---

# Phase 4 — Security & Penetration Testing

### 4.1 Authentication & Authorization

| Test | Command / Technique | Expected Result |
|------|---------------------|-----------------|
| **JWT Forgery** | Craft JWT with wrong `AUTH_SECRET` | 401 Unauthorized |
| **Expired Token** | Use token past `ACCESS_TOKEN_TTL_SECONDS` | 401 Unauthorized |
| **Refresh Token Replay** | Reuse a rotated refresh token | 401 + entire chain revoked |
| **CSRF on Mutation** | `POST` without `X-CSRF-Token` header | 403 Forbidden |
| **Cookie Theft Mitigation** | Inspect cookies for `HttpOnly`, `Secure`, `SameSite` | All set correctly |
| **Admin Escalation** | Customer token on `GET /admin/users` | 403 Forbidden |
| **Cross-Tenant Data** | User A tries `GET /v1/jobs/{user_B_job_id}` | 404 Not Found |
| **Password Brute Force** | 6+ rapid login attempts with wrong password | 429 Too Many Requests |
| **Account Enumeration** | Login with non-existent email | Same error as wrong password |

### 4.2 Input Validation & Injection

| Test | Payload | Expected Result |
|------|---------|-----------------|
| **SQL Injection** | `{"email": "' OR 1=1 --"}` | 422 Validation Error |
| **XSS in Job Name** | `{"prompt": "<script>alert(1)</script>"}` | Stored safely, never executed |
| **Path Traversal** | Artifact key `../../etc/passwd` | `ValueError: invalid_object_key` |
| **Oversized Payload** | `Content-Length: 50000000` | 413 Request Too Large |
| **Unsupported Content-Type** | `Content-Type: application/x-executable` | 415 Unsupported Media Type |

### 4.3 HMAC Agent Protocol Security

| Test | Technique | Expected Result |
|------|-----------|-----------------|
| **Bit-Flip Attack** | Flip 1 bit in heartbeat payload after signing | 401 (HMAC mismatch) |
| **Replay Attack** | Resend identical signed heartbeat 5 minutes later | 401 (stale timestamp, `MACHINE_HMAC_SKEW_SECONDS=300`) |
| **Clock Skew Attack** | Set agent timestamp to `now + 600s` | 401 (future timestamp rejected) |
| **Cross-Machine Claim** | Machine A's secret used to claim Machine B's job | 403 Forbidden |
| **Unknown Machine** | Random UUID in heartbeat | 401 (machine_id not found) |

### 4.4 Financial Security

| Test | Scenario | Expected Result |
|------|----------|-----------------|
| **Double-Spend** | Submit 5×$10 jobs with $10 balance concurrently | Exactly 1 succeeds, 4 return `409 insufficient_balance` |
| **Negative Balance** | Direct SQL `UPDATE` to set balance = -1 | PostgreSQL `CHECK` constraint rejects |
| **Refund Overflow** | Attempt to refund more than original charge | `409 refund_exceeds_cumulative_cap` |
| **Phantom Credits** | Forge Stripe webhook with valid-looking payload | Rejected: signature verification fails |
| **Metering Fraud** | Complete a job in 0.1s, bill for 0.1s | `BILLING_MIN_BILLABLE_SECONDS=60` floors to 60s |

---

# Phase 5 — Performance & Stress Testing

### 5.1 API Latency Benchmarks
```bash
# Use wrk or vegeta for HTTP load testing
wrk -t4 -c50 -d30s https://stocks-production-76fe.up.railway.app/health

# Key endpoints to benchmark:
# GET  /v1/marketplace/machines   (search, must be fast)
# POST /v1/quotes                 (pricing math)
# POST /v1/jobs                   (scheduler + reserve)
# POST /agents/machines/{id}/claim (agent polling)
```

**Acceptance Criteria:**
- P50 latency < 100ms for read endpoints
- P99 latency < 500ms under normal load
- Zero HTTP 5xx under 50 concurrent connections
- Heartbeat ingestion < 50ms (critical for `ONLINE_WINDOW_SECONDS=90`)

### 5.2 Database Connection Pool
```bash
# Check active connections against pool limits
psql -c "SELECT count(*) FROM pg_stat_activity WHERE datname = 'postgres'"

# Monitor for connection leaks under load
# Watch for: "FATAL: too many connections for role"
```

### 5.3 Redis Stress
```bash
# Verify rate limiter doesn't leak keys
redis-cli DBSIZE  # Before
# Fire 100 login attempts
redis-cli DBSIZE  # After — keys should expire via TTL
```

---

# Phase 6 — Live Production E2E Testing

### 6.1 Full Production Smoke Test Matrix

Run against the **live Railway deployment** (`https://stocks-production-76fe.up.railway.app`):

| # | Test Flow | Command | Pass Criteria |
|---|-----------|---------|---------------|
| 1 | Health Check | `curl -s $API/health \| jq .status` | `"ok"` |
| 2 | Provider Enrollment | `phase2_provider_e2e.py` | `ALL PHASE-2 END-TO-END CHECKS PASSED` |
| 3 | Concurrency Invariant | `phase3_concurrency_e2e.py` | Exactly 1 of N reserves succeeds |
| 4 | Job Execution Lifecycle | `phase4_execution_e2e.py` | Job completes with verified artifacts |
| 5 | Billing Ledger Integrity | `phase5_billing_e2e.py` | Double-entry balances, no negatives |
| 6 | Web Build | `npm run build` | 46/46 routes, 0 TypeScript errors |
| 7 | Contract Tests | `npx hardhat test` | 12/12 Solidity tests pass |

### 6.2 Cross-Browser Frontend Testing
Test the web application at the production URL across:
- Chrome (latest), Firefox (latest), Safari (latest)
- Mobile: iOS Safari, Android Chrome
- Screen readers: NVDA / VoiceOver

**Critical UI Flows to Test:**
1. Register → Verify Email → Login → Dashboard
2. Dashboard → Browse Marketplace → Get Quote → Reserve GPU
3. Dashboard → Submit Job → Monitor Progress → Download Results
4. Provider → Onboarding → Enroll Machine → View Earnings
5. Admin → User Management → Suspend/Reinstate User
6. Billing → Add Credits (Stripe) → View Invoices → Request Payout

---

# Phase 7 — Frontend Visual & Accessibility Audit

### 7.1 Responsive Design Checklist

| Viewport | Width | Critical Pages |
|----------|-------|----------------|
| Mobile S | 320px | Login, Register, Dashboard |
| Mobile L | 425px | Marketplace, Job Details |
| Tablet | 768px | Provider Dashboard, Admin |
| Desktop | 1440px | All pages |
| Ultra-wide | 2560px | Marketplace Grid |

### 7.2 Accessibility (WCAG 2.1 AA)
- [ ] All images have `alt` text
- [ ] Color contrast ratio ≥ 4.5:1 for normal text
- [ ] All interactive elements are keyboard-navigable
- [ ] Form labels are properly associated with inputs
- [ ] Error states are announced to screen readers
- [ ] No content flashes faster than 3 times per second

### 7.3 Performance (Core Web Vitals)
- **LCP (Largest Contentful Paint):** < 2.5s
- **FID (First Input Delay):** < 100ms
- **CLS (Cumulative Layout Shift):** < 0.1
- **First Load JS** should not exceed 200kB per route

---

# Phase 8 — Smart Contract Audit

### 8.1 Solidity-Specific Checks

| Check | Rationale |
|-------|-----------|
| **Reentrancy Guards** | `settle()` must use `nonReentrant` or checks-effects-interactions |
| **Integer Overflow** | Solidity 0.8+ reverts on overflow, but verify `unchecked` blocks |
| **Access Control** | `onlyOwner` on `pause()`, `setFeeRate()`, `updateTreasury()` |
| **Front-Running Mitigation** | Escrow keyed by `keccak256(customer, jobId)` prevents mempool sniping |
| **Gas Limits** | No unbounded loops; fixed-size arrays only |
| **Timestamp Dependence** | `block.timestamp` used for deadline — 15s miner variance is acceptable |
| **Self-Destruct** | Contract must NOT contain `selfdestruct` |
| **Signature Malleability** | EIP-712 typed-data signing prevents cross-chain replay |

### 8.2 Deployment Verification
```bash
# Verify deployed bytecode matches compiled artifacts
cd contracts && npx hardhat verify --network sepolia $CONTRACT_ADDRESS

# Check contract is not upgradeable-proxy (if not intended)
# Check owner is a multisig (not a single EOA)
```

---

# Phase 9 — Infrastructure & Deployment Audit

### 9.1 Docker Container Security

| Check | Command | Expected |
|-------|---------|----------|
| Non-root user | `docker inspect --format='{{.Config.User}}' api` | `appuser` |
| No `--privileged` | Check `docker-compose.prod.yml` | Not present |
| Read-only filesystem | Check `read_only: true` where feasible | Applied to stateless services |
| Health check defined | Check `healthcheck` in compose | `GET /health` every 30s |
| Resource limits | Check `mem_limit`, `cpus` | Configured per service |
| Secret injection | Check secrets are via env vars, NOT baked into image | No secrets in Dockerfile |

### 9.2 Railway Production Config

| Variable | Required | Sensitive | Validated |
|----------|----------|-----------|-----------|
| `DATABASE_URL` | ✅ | ✅ | Connection tested at boot |
| `REDIS_URL` | ✅ | ✅ | Used for rate limiting |
| `AUTH_SECRET` | ✅ | ✅ | Rejected if dev default in prod |
| `SESSION_SECRET` | ✅ | ✅ | Rejected if dev default in prod |
| `EMAIL_PROVIDER` | ✅ | ❌ | `resend` or `smtp` in prod |
| `RESEND_API_KEY` | Conditional | ✅ | Required when provider=resend |
| `STRIPE_SECRET_KEY` | Conditional | ✅ | Required when payments=stripe |
| `STRIPE_WEBHOOK_SECRET` | Conditional | ✅ | Required for webhook validation |
| `PLATFORM_SIGNER_PRIVATE_KEY` | ✅ | ✅ | Rejected if dev default in prod |
| `FRONTEND_URL` | ✅ | ❌ | Used for CORS origins |

### 9.3 SSL / TLS
```bash
# Verify HTTPS with proper certificate
curl -vvv https://stocks-production-76fe.up.railway.app/health 2>&1 | grep "SSL certificate"

# Check for HSTS header
curl -sI https://stocks-production-76fe.up.railway.app/ | grep -i strict

# No mixed content (HTTP resources on HTTPS pages)
```

### 9.4 Logging & Observability
- [ ] Structured JSON logging in production (`STRUCTURED_LOGGING=true`)
- [ ] No PII in logs (no passwords, no full credit card numbers)
- [ ] Request ID propagation for distributed tracing
- [ ] Sentry configured with `send_default_pii=False`
- [ ] Log levels: ERROR for failures, WARN for degraded, INFO for lifecycle

---

# Phase 10 — Data Integrity & Migration Audit

### 10.1 Migration Chain Verification
```bash
cd apps/api

# Show the migration chain (must be linear, no forks)
.venv/bin/alembic history --verbose

# Expected chain:
# 0001_initial          → users, sessions, api_keys, audit_logs
# 0002_provider_phase2  → providers, machines, enrollment_tokens
# 0003_marketplace_phase3 → pricing_rules, quotes, reservations
# 0004_jobs_phase4      → model_registry, jobs, events, artifacts
# 0005_billing_phase5   → billing_accounts, ledger_entries, invoices, payouts
# 0006_isolation_phase8 → isolation tier columns
# 0007_telemetry_phase9 → telemetry_samples, telemetry_alerts
# 0008_settlement_phase10 → escrows, vouchers, disputes
# 0009_hardware_model_phase4 → hardware job columns
# 0010_stripe_billing_phase5 → stripe payment columns

# Verify current head matches latest migration
.venv/bin/alembic current
```

### 10.2 Database Constraint Verification
```sql
-- Verify non-negative balance constraint exists
SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
WHERE conrelid = 'billing_accounts'::regclass AND contype = 'c';

-- Verify foreign key cascades for job cleanup
SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
WHERE conrelid = 'artifacts'::regclass AND contype = 'f';

-- Verify unique constraints on idempotency keys
SELECT indexname FROM pg_indexes
WHERE tablename = 'jobs' AND indexdef LIKE '%idempotency_key%';
```

### 10.3 Orphaned Data Detection
```sql
-- Jobs without a customer (broken FK)
SELECT COUNT(*) FROM jobs j
LEFT JOIN users u ON j.customer_id = u.id WHERE u.id IS NULL;

-- Artifacts without a parent job
SELECT COUNT(*) FROM artifacts a
LEFT JOIN jobs j ON a.job_id = j.id WHERE j.id IS NULL;

-- Ledger entries with broken account reference
SELECT COUNT(*) FROM ledger_entries le
LEFT JOIN billing_accounts ba ON le.account_id = ba.id WHERE ba.id IS NULL;

-- Machines without a provider
SELECT COUNT(*) FROM machines m
LEFT JOIN providers p ON m.provider_id = p.id WHERE p.id IS NULL;
```

---

# Appendix A — Bugs Found & Fixed (This Audit)

### Bug #1: Email Test Uses Live Resend API Instead of Console
- **Severity:** Medium (CI/CD failure)
- **Root Cause:** `conftest.py` uses `os.environ.setdefault()` which does NOT override the local `.env` file's `EMAIL_PROVIDER=resend`. Pydantic-settings loads `.env` first.
- **Impact:** `test_email_console_dispatch` sends a real Resend API call to `testuser@example.com`, which Resend rejects with HTTP 422 on free tier.
- **Fix:**
  1. Added `os.environ.setdefault("EMAIL_PROVIDER", "console")` to `conftest.py`
  2. Wrapped `test_email_console_dispatch` in `settings.EMAIL_PROVIDER = "console"` with `finally` restore

### Bug #2: Artifact Upload HTTP 500 in Docker (Permission Denied)
- **Severity:** Critical (data loss)
- **Root Cause:** Dockerfile creates `appuser` (UID 10001) but the `/app` directory is owned by `root`. `LocalFsStore` tries to create `./.artifact_store/` and gets `PermissionError`.
- **Impact:** Agent artifact upload fails with HTTP 500, job is stuck in `running` state forever, customer charged for incomplete work.
- **Fix:**
  1. Updated Dockerfile: `mkdir -p /app/.artifact_store && chown -R appuser:appuser /app`
  2. Added self-healing fallback in `LocalFsStore.__init__()` to `/tmp/computecoin_artifacts`

### Bug #3: E2E Tests Crash on Production (Missing Dev Token)
- **Severity:** Low (test-only, no production impact)
- **Root Cause:** All four E2E test scripts use `r.json()["email_verification_token"]` which raises `KeyError` in production where `expose_dev_tokens` is `False`.
- **Impact:** E2E scripts cannot run against the live production API.
- **Fix:** Changed all four files to use `.get("email_verification_token")` with a conditional verify step.

---

# Appendix B — Bug Resolution Methodology

### The 7-Step Debugging Framework

#### Step 1: Reproduce Reliably
```
"If you can't reproduce it, you can't fix it."
```
- Run the exact failing test in isolation: `pytest tests/test_X.py::test_name -v --tb=long`
- Capture the FULL stack trace, not just the assertion error
- Note the environment: Python version, OS, database, Redis state

#### Step 2: Isolate the Blast Radius
```
"Is this a single bug, or a symptom of a systemic failure?"
```
- Run the ENTIRE test suite — if 1 test fails, it's localized; if 15 fail, it's environmental
- Check `conftest.py` fixtures — are they properly isolated per-test?
- Check for global state mutation (singletons, module-level caches like `@lru_cache`)

#### Step 3: Read the Error, Not the Test
```
"The test tells you WHAT failed. The error tells you WHY."
```
- `assert False is True` → the function returned False → trace the function
- `422 Unprocessable Entity` → validation error → check the exact field
- `500 Internal Server Error` → unhandled exception → check server logs, not the test

#### Step 4: Trace the Data Flow
```
"Follow the money. Follow the bytes."
```
- For API bugs: Request → Router → Service → Repository → Database → Response
- For agent bugs: Config → Enrollment → Heartbeat → Job Claim → Executor → Result
- For billing bugs: Hold → Reserve → Execute → Meter → Capture → Ledger

#### Step 5: Fix the Root Cause, Not the Symptom
```
"A bandaid that masks the error is worse than the error itself."
```
- BAD: `try: ... except: pass` in the upload handler
- GOOD: Fix the filesystem permission so the upload succeeds
- BEST: Fix the Dockerfile AND add a self-healing fallback AND add a test

#### Step 6: Verify the Fix Doesn't Break Others
```
"Every fix is a potential regression."
```
- Run the FULL test suite after the fix, not just the one test
- Run the provider-agent tests too — they share the HMAC contract
- Run the frontend build — a config change could break TypeScript types

#### Step 7: Document for the Next Engineer
```
"Your future self is another person."
```
- Add a comment explaining WHY, not WHAT (the code shows what)
- Update this QA document with the new edge case
- Add the regression test permanently to the suite

### Full-Stack Resolution Patterns

#### Pattern: Environment-Sensitive Configuration
```python
# BAD: os.environ.setdefault() — does NOT override .env files loaded by pydantic
os.environ.setdefault("EMAIL_PROVIDER", "console")

# GOOD: Explicitly set on the settings object with restore
orig = settings.EMAIL_PROVIDER
try:
    settings.EMAIL_PROVIDER = "console"
    # ... test code ...
finally:
    settings.EMAIL_PROVIDER = orig
```

#### Pattern: Graceful Degradation in Storage
```python
# BAD: Crash on PermissionError
os.makedirs(self.root, exist_ok=True)  # raises if /app is root-owned

# GOOD: Self-heal to a writable location
try:
    os.makedirs(self.root, exist_ok=True)
    # test write permission
except (PermissionError, OSError):
    self.root = "/tmp/computecoin_artifacts"
    os.makedirs(self.root, exist_ok=True)
```

#### Pattern: Production-Safe API Response Parsing
```python
# BAD: Hard key access (crashes in production)
token = response.json()["email_verification_token"]

# GOOD: Graceful optional access
token = response.json().get("email_verification_token")
if token:
    verify(token)
```

#### Pattern: Docker Non-Root File Ownership
```dockerfile
# BAD: Files owned by root, process runs as appuser
RUN useradd appuser
USER appuser
# appuser can't write to /app

# GOOD: Transfer ownership before switching user
RUN useradd appuser \
    && mkdir -p /app/.artifact_store \
    && chown -R appuser:appuser /app
USER appuser
```

---

# Appendix C — Full Test Execution Commands

### Complete Audit Run (Copy-Paste Ready)

```bash
#!/bin/bash
set -e
cd /home/root509/stocks
echo "=== ComputeCoin Full QA Audit ==="

echo ""
echo "--- 1/5: Backend API Tests ---"
cd apps/api && .venv/bin/python -m pytest tests/ -v --tb=short -q
echo ""

echo "--- 2/5: Provider Agent Tests ---"
cd ../../provider-agent && .venv/bin/python -m pytest tests/ -v --tb=short -q
echo ""

echo "--- 3/5: Smart Contract Tests ---"
cd ../contracts && npx hardhat test
echo ""

echo "--- 4/5: Frontend Build ---"
cd ../apps/web && npm run build
echo ""

echo "--- 5/5: Live Production E2E ---"
cd ../..
export CC_API_BASE=https://stocks-production-76fe.up.railway.app
COMPUTECOIN_CONFIG_DIR=$(mktemp -d) \
  provider-agent/.venv/bin/python tests/end-to-end/phase2_provider_e2e.py
COMPUTECOIN_CONFIG_DIR=$(mktemp -d) CC_CONCURRENCY=4 \
  provider-agent/.venv/bin/python tests/end-to-end/phase3_concurrency_e2e.py

echo ""
echo "=== ALL AUDIT PHASES COMPLETED ==="
```

### Expected Final Results

```
┌───────────────────────────────┬────────┬────────┬─────────┐
│ Component                     │ Passed │ Failed │ Skipped │
├───────────────────────────────┼────────┼────────┼─────────┤
│ Backend API (apps/api)        │    166 │      0 │       0 │
│ Provider Agent                │     66 │      0 │       1 │
│ Smart Contracts (Solidity)    │     12 │      0 │       0 │
│ Frontend Build (Next.js)      │  46/46 │      0 │       0 │
│ Live E2E Phase 2 (Provider)   │  17/17 │      0 │       0 │
│ Live E2E Phase 3 (Concurrency)│  10/10 │      0 │       0 │
├───────────────────────────────┼────────┼────────┼─────────┤
│ TOTAL                         │    317 │      0 │       1 │
└───────────────────────────────┴────────┴────────┴─────────┘
```

---

> **Final Note:** This document is a living artifact. Every bug found in future audits
> should be appended to Appendix A with root cause analysis, and every new test pattern
> should be added to the appropriate phase section. The goal is not perfection — it is
> **continuous, documented improvement** with full traceability.
