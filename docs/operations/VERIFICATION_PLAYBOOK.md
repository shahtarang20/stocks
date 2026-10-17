# ComputeCoin — End-to-End Verification & Change-Review Playbook

> How to verify the whole system end-to-end, and how to review a set of changes
> (a commit / PR / agent handoff) end-to-end — with copy-paste **master prompts**,
> the exact commands, and the evidence each step must produce.
>
> Core rule (from the project spec): **never claim something works unless its
> implementation and a test demonstrate it.** Agent reports are a lead, not proof —
> re-read the source and re-run the suite yourself.

---

## 0. Principles (apply to every verification)
1. **Verify in source, then run it.** Read the actual lines (`file:line`), don't trust a summary.
2. **No fabrication.** Paste real output; if something is skipped/failing, say so.
3. **Money & correctness first.** A balance/authorization bug outranks everything.
4. **Prove invariants, don't assert them.** No-double-book, no-double-charge, no-negative-balance, ledger-balances must be shown under real conditions (Postgres, cross-process), not just unit mocks.
5. **A passing mocked test ≠ real execution.** GPU/payment/chain paths need a real integration run.
6. **Never weaken a check to make a test pass.** If a test encoded the bug, fix the test to the secure behavior.

---

## 1. Verify the whole system end-to-end

### 1a. The command matrix (fast gate)
```bash
# Backend API (offline, SQLite)
cd apps/api && .venv/bin/pytest -q

# Provider agent
cd provider-agent && .venv/bin/pytest -q

# SDKs
cd sdk/python && .venv/bin/pytest -q
cd sdk/typescript && npx tsc --noEmit && npx vitest run

# Smart contracts
cd contracts && npx hardhat test

# Web
cd apps/web && npx tsc --noEmit && npm run build

# Migrations apply on a clean DB
cd apps/api && rm -f /tmp/v.db && DATABASE_URL="sqlite+aiosqlite:////tmp/v.db" .venv/bin/alembic upgrade head
```
✅ Evidence: real pass counts for every suite + a green build + migrations applying.

### 1b. Live end-to-end on REAL Postgres (the real proof)
The committed scripts under `tests/end-to-end/` are the acceptance tests. Run them
against a throwaway Postgres and a live API (no Docker needed — initdb a userspace
cluster):
```bash
BIN=/usr/lib/postgresql/16/bin; PG=/tmp/vpg
rm -rf $PG && "$BIN/initdb" -D $PG -U ccuser --auth=trust -A trust >/dev/null
"$BIN/pg_ctl" -D $PG -o "-p 5544 -k /tmp -c listen_addresses=127.0.0.1" -l /tmp/pg.log start
"$BIN/createdb" -h 127.0.0.1 -p 5544 -U ccuser cc_e2e

cd apps/api
export DATABASE_URL="postgresql+asyncpg://ccuser@127.0.0.1:5544/cc_e2e"
export ENVIRONMENT=development AUTH_SECRET=$(openssl rand -hex 32) SESSION_SECRET=$(openssl rand -hex 32)
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8099   # (backgrounded)

# then, with the provider-agent venv (has computecoin_agent + httpx):
CC_API_BASE=http://127.0.0.1:8099 COMPUTECOIN_CONFIG_DIR=$(mktemp -d) \
  provider-agent/.venv/bin/python -I tests/end-to-end/phase2_provider_e2e.py
#   ... phase4_execution_e2e.py, phase5_billing_e2e.py, etc.
```
✅ Evidence: every `[PASS]` line + `ALL ... CHECKS PASSED`.

### 1c. The hard invariants (prove under real contention)
- **No GPU double-booking** — `tests/end-to-end/phase3_concurrency_e2e.py`: fire N
  concurrent reservations across **two independent uvicorn processes** on one
  Postgres → exactly one `201`, rest `409 no_capacity`; confirm at the DB:
  `SELECT sum(slots) ... WHERE status='active'` == max_concurrent.
- **No double charge / no negative balance** — `phase5_billing_e2e.py`: a customer
  funded for one job submits two jobs across two processes → one `201`, one
  `402 insufficient_credits`; `SELECT min(balance_minor) FROM billing_accounts WHERE owner_type='customer'` ≥ 0.
- **Ledger balances** — `GET /v1/admin/reconciliation` → `balanced=true, imbalance="0.00"`
  AND DB `sum(debits)==sum(credits)` over `ledger_entries`.
- **Disconnected provider never strands a job** — claim a job, let the lease expire,
  confirm it is recovered (not stuck RUNNING) and the hold is released.
> Two independent processes matter: an in-process lock would mask the bug; only the
> Postgres `SELECT ... FOR UPDATE` can serialize across processes.

### 1d. Teardown
```bash
pkill -9 -f "uvicorn app.main:app"; /usr/lib/postgresql/16/bin/pg_ctl -D /tmp/vpg stop -m immediate
rm -rf /tmp/vpg /tmp/pg.log
```

### Master prompt — full system verification
> Verify the ComputeCoin monorepo end-to-end. (1) Run every test suite
> (apps/api pytest, provider-agent, sdk/python, sdk/typescript tsc+vitest,
> contracts hardhat) and the web tsc+build; paste REAL pass counts. (2) Apply
> Alembic migrations on a clean DB. (3) Stand up a throwaway Postgres + a live API
> and run every tests/end-to-end/*.py script; paste the PASS lines. (4) Prove the
> hard invariants under real cross-process contention: no GPU double-booking, no
> double-charge, no negative balance, ledger reconciliation imbalance == 0, and a
> disconnected-provider job is recovered — each confirmed at the DB level, not just
> via the API. Tear down the throwaway infra. Report actual results only; if
> anything fails or is skipped, say so. Do not fabricate output.

---

## 2. Review a set of changes end-to-end (commit / PR / agent handoff)

### 2a. See exactly what changed
```bash
git log --oneline -5
git status --short                 # uncommitted drift?
git --no-pager show <sha> --stat   # files in the change
git --no-pager show <sha> -- <path># per-file diff (read EVERY security-relevant one)
```

### 2b. Read every change with these questions
- **AuthZ / IDOR:** does each resource-by-id path filter by owner (or 404)? are
  privileged routes gated by `require_roles(...)`? did a new router skip it?
- **Money:** does every ledger write go through the balanced, idempotent
  `post_transaction` choke point? can anything double-charge / over-refund / drive
  an account negative?
- **Concurrency:** are check-and-write paths under a row lock? can recovery/claim
  double-process?
- **Cross-component contracts:** do the two sides agree on the exact wire shape?
  (e.g. backend voucher packing vs. the Solidity `settleJob` hash — byte-for-byte;
  relative vs. absolute URLs; header/token names). This is where unit-green-but-
  broken bugs hide.
- **Schema vs. migration:** every column a model/handler references must exist in a
  migration (tests using `create_all` will pass even if the migration is missing it —
  so apply the migration on a clean DB and diff the columns).
- **Tests:** does each fix have a test that FAILS without it? Were any tests changed
  to encode the old (vulnerable) behavior? (Allowed only when correcting to the
  secure behavior.)

### 2c. Prove it
- Run 2c... the full matrix (§1a) and the relevant live e2e (§1b/§1c).
- For a schema change: `alembic upgrade head` on a fresh DB, then `pragma table_info`
  / `\d` to confirm the new columns exist.
- For a cross-component change: run the real end-to-end that exercises both sides.

### 2d. Verdict format
Report a table: `finding | file:line | status (verified/▲note/✗broken) | evidence`.
State one of: **Correct & green**, **Correct with minor non-blocking notes**, or
**Broken — <what> fails**. Never say "perfect" without the evidence behind it.

### Master prompt — end-to-end change review
> Review commit/PR <ref> in the ComputeCoin repo end-to-end. First `git show --stat`
> it and read EVERY changed file's diff at file:line — do not trust the commit
> message. For each change assess: authorization/IDOR, money-ledger safety
> (balanced + idempotent + no-negative), concurrency/row-locking, and
> cross-component wire-contract agreement (e.g. backend signature packing must match
> the Solidity hash byte-for-byte; cookie/CSRF attributes; column names). Confirm
> every column referenced by models/handlers exists in a migration by applying
> migrations on a CLEAN database and inspecting the schema. Then run the full test
> matrix and the relevant tests/end-to-end live scripts on real Postgres, including
> the cross-process invariant proofs. Flag any test that was changed to match the
> vulnerable behavior. Produce a verdict table (finding | file:line | verified/
> note/broken | evidence) and a one-line overall verdict backed by real output.
> No fabrication; if you cannot prove it, say so.

---

## 3. Evidence log template
```
Suite results:      API __ / agent __ / sdk-py __ / sdk-ts __ / contracts __ / web build __
Migrations:         upgrade head OK on clean DB (schema cols verified: ____)
Live e2e:           phase2 ✅  phase3(concurrency) ✅  phase4 ✅  phase5(billing) ✅
Invariants:         no-double-book ✅  no-double-charge ✅  no-neg-balance ✅  recon imbalance=0.00 ✅  lease-recovery ✅
Change review:      <N> files read; <M> findings; verdict: ____
```

_Keep this playbook next to [AUDIT_REPORT.md](../../AUDIT_REPORT.md) and
[LAUNCH_PLAN.md](../../LAUNCH_PLAN.md)._
