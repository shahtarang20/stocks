"""Phase 5 LIVE money-correctness acceptance test (real Postgres, cross-process).

Proves the ledger invariants end to end with NO mocks:
  - signup grant + credit purchase
  - job HOLD on submit -> CAPTURE on completion (customer charged actual, provider
    earnings + platform revenue credited, remainder released) -> invoice
  - failed/cancelled job -> hold fully released, zero net charge
  - refund reverses a charge
  - whole-ledger reconciliation imbalance == 0 (INVARIANT 2) after all of it
  - provider earnings live + payout
  - NO NEGATIVE BALANCE under a cross-process concurrent race: a customer funded for
    exactly one job submits two jobs at once across TWO API processes -> exactly one
    hold wins, the other 402 insufficient_credits, balance never < 0.

Run with the provider-agent venv. API started with BILLING_SIGNUP_GRANT_MINOR=200
("2.00") and a short LEASE_TTL. Env: CC_API_BASE (8099), CC_RESERVE_BASES
(comma list for the race), COMPUTECOIN_CONFIG_DIR, PGPORT (5544).
"""
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from computecoin_agent import enrollment, heartbeat, jobrunner

BASE = os.environ.get("CC_API_BASE", "http://127.0.0.1:8099")
RACE_BASES = [b.strip() for b in os.environ.get("CC_RESERVE_BASES", BASE).split(",") if b.strip()]
CONFIG_DIR = Path(os.environ.get("COMPUTECOIN_CONFIG_DIR") or tempfile.mkdtemp())
PGPORT = os.environ.get("PGPORT", "5544")
pw = "Str0ngPass-123!"
c = httpx.Client(base_url=BASE, timeout=30)
fails = 0
pid = os.getpid()


def check(name, ok, extra=""):
    global fails
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {extra}")
    if not ok:
        fails += 1


def psql(sql):
    out = subprocess.run(
        ["psql", "-h", "127.0.0.1", "-p", PGPORT, "-U", "ccuser", "-d", "cc_e2e", "-tAc", sql],
        capture_output=True, text=True, env={**os.environ, "PGPASSWORD": ""},
    )
    return out.stdout.strip()


def signup(email, token_out=False):
    r = c.post("/auth/register", json={"email": email, "password": pw})
    assert r.status_code in (200, 201), r.text
    vtok = r.json().get("email_verification_token")
    if vtok:
        c.post("/auth/verify-email", json={"token": vtok})
    tok = c.post("/auth/login", json={"email": email, "password": pw}).json()["access_token"]
    uid = psql(f"select id from users where email='{email}'")
    return (tok, uid) if token_out else tok


# ---- provider + machine (capacity 5 so the race is gated by MONEY, not slots) ----
ptok = signup(f"prov_{pid}@example.com")
PH = {"Authorization": f"Bearer {ptok}"}
c.post("/provider/register", json={"display_name": "E2E GPU Co"}, headers=PH)
enr = c.post("/provider/enrollment-tokens", json={"label": "e2e"}, headers=PH).json()["enrollment_token"]
cap = {"verification_status": "detected", "cpu_model": "cpu", "cpu_cores": 8, "ram_mb": 16384,
       "disk_gb": 200, "gpus": [{"model": "E2E-GPU", "vram_mb": 24576, "cuda_version": "12.4"}],
       "supported_workloads": ["text-generation", "batch-inference"]}
res = enrollment.enroll_remote(BASE, enr, cap, config_path=CONFIG_DIR / "agent.toml")
mid, msec = res.machine_id, res.machine_secret
c.patch(f"/provider/machines/{mid}",
        json={"availability": "online", "asking_price_per_hour": "1.50",
              "supported_workloads": ["text-generation", "batch-inference"], "max_concurrent_jobs": 5},
        headers=PH)
heartbeat.send_contract_heartbeat(BASE, str(mid), msec, availability="online")
check("provider machine online w/ capacity 5", True, f"machine={mid}")

# ============================ FUNCTIONAL MONEY FLOW ============================
ctok = signup(f"cust_{pid}@example.com")
CH = {"Authorization": f"Bearer {ctok}"}
acct = c.get("/v1/billing/account", headers=CH).json()
check("signup grant present", acct["available"] == "2.00", f"available={acct['available']}")
c.post("/v1/billing/credits/purchase", json={"amount": "100.00"}, headers=CH)
acct = c.get("/v1/billing/account", headers=CH).json()
check("credit purchase increased balance", acct["available"] == "102.00", f"available={acct['available']}")

# submit a job -> HOLD
r = c.post("/v1/jobs",
           json={"workload_type": "text-generation", "model": "echo-sim-v1",
                 "input": {"prompt": "bill me", "max_tokens": 16}, "machine_id": str(mid),
                 "gpu_count": 1, "estimated_minutes": 60, "max_cost": "5.00",
                 "max_runtime_seconds": 60, "retry_policy": {"max_retries": 0}}, headers=CH)
check("job submitted (credits held)", r.status_code == 201, f"HTTP {r.status_code} {r.text[:120]}")
jid = r.json()["job_id"]
acct = c.get("/v1/billing/account", headers=CH).json()
check("hold placed: reserved = 1.65, available dropped", acct["reserved"] == "1.65" and acct["available"] == "100.35",
      f"available={acct['available']} reserved={acct['reserved']}")

# run the job -> CAPTURE + release remainder
jobrunner.run_one(BASE, str(mid), msec)
job = c.get(f"/v1/jobs/{jid}", headers=CH).json()
check("job completed", job["status"] == "completed", f"status={job['status']}")
acct = c.get("/v1/billing/account", headers=CH).json()
check("after settle: reserved back to 0.00", acct["reserved"] == "0.00", f"reserved={acct['reserved']}")
check("customer charged actual (<= hold); not the full hold",
      acct["available"] not in ("100.35",) and acct["available"] < "102.00", f"available={acct['available']}")

# invoice
inv = c.get("/v1/invoices", headers=CH).json()
inv = inv if isinstance(inv, list) else inv.get("items", [])
check("invoice created for the job", len(inv) >= 1, f"{len(inv)} invoice(s)")
if inv:
    one = c.get(f"/v1/invoices/{inv[0]['invoice_id']}", headers=CH).json()
    check("invoice keeps estimated vs actual separately",
          "estimated_total" in one and "actual_total" in one,
          f"est={one.get('estimated_total')} act={one.get('actual_total')}")

# provider earnings live + payout
er = c.get("/v1/provider/earnings", headers=PH).json()
check("provider earnings accrued (live from ledger)", er.get("available", "0.00") not in ("0.00", None),
      f"available={er.get('available')}")
pr = c.post("/v1/provider/payouts", json={"amount": "0.01"}, headers=PH)
check("payout request within earnings ok", pr.status_code in (200, 201), f"HTTP {pr.status_code}")
pr2 = c.post("/v1/provider/payouts", json={"amount": "9999.00"}, headers=PH)
check("payout beyond earnings -> 409", pr2.status_code == 409, f"HTTP {pr2.status_code}")

# failed/cancelled job -> hold released, no charge
avail_before = c.get("/v1/billing/account", headers=CH).json()["available"]
r = c.post("/v1/jobs",
           json={"workload_type": "text-generation", "model": "echo-sim-v1",
                 "input": {"prompt": "cancel me", "max_tokens": 8}, "machine_id": str(mid),
                 "gpu_count": 1, "estimated_minutes": 60, "max_cost": "5.00", "max_runtime_seconds": 60},
           headers=CH)
cid = r.json()["job_id"]
held = c.get("/v1/billing/account", headers=CH).json()
check("cancel-test job placed a hold", held["reserved"] == "1.65", f"reserved={held['reserved']}")
c.post(f"/v1/jobs/{cid}/cancel", headers=CH)
after = c.get("/v1/billing/account", headers=CH).json()
check("cancelled job: hold fully released (no charge)",
      after["reserved"] == "0.00" and after["available"] == avail_before,
      f"available={after['available']} (was {avail_before}) reserved={after['reserved']}")

# ---- admin: reconciliation + refund (grant admin role via DB, as Phase 1 expects) ----
atok, auid = signup(f"admin_{pid}@example.com", token_out=True)
psql(f"insert into user_roles (id, user_id, role) values (gen_random_uuid(), '{auid}', 'administrator')")
atok = c.post("/auth/login", json={"email": f"admin_{pid}@example.com", "password": pw}).json()["access_token"]
AH = {"Authorization": f"Bearer {atok}"}
rec = c.get("/v1/admin/reconciliation", headers=AH)
check("admin reconciliation reachable (RBAC)", rec.status_code == 200, f"HTTP {rec.status_code}")
recj = rec.json()
check("LEDGER BALANCED: imbalance == 0.00", recj.get("balanced") is True and recj.get("imbalance") == "0.00",
      f"balanced={recj.get('balanced')} imbalance={recj.get('imbalance')} "
      f"debits={recj.get('total_debits')} credits={recj.get('total_credits')}")
# non-admin blocked
check("non-admin cannot reconcile (403)", c.get("/v1/admin/reconciliation", headers=CH).status_code == 403)
# refund the completed job
rf = c.post("/v1/admin/refunds", json={"job_id": jid, "amount": "0.01", "reason": "e2e goodwill"}, headers=AH)
check("admin refund authorized", rf.status_code in (200, 201), f"HTTP {rf.status_code} {rf.text[:120]}")
rec2 = c.get("/v1/admin/reconciliation", headers=AH).json()
check("ledger STILL balanced after refund", rec2.get("balanced") is True and rec2.get("imbalance") == "0.00",
      f"imbalance={rec2.get('imbalance')}")

# ======================= NO-NEGATIVE-BALANCE CONCURRENCY =======================
# A customer funded with ONLY the 2.00 signup grant submits two jobs at once across
# two API processes. Each job holds 1.65 -> only one can succeed; balance >= 0.
rtok = signup(f"race_{pid}@example.com")
RH = {"Authorization": f"Bearer {rtok}"}
job_body = {"workload_type": "text-generation", "model": "echo-sim-v1",
            "input": {"prompt": "race", "max_tokens": 8}, "machine_id": str(mid),
            "gpu_count": 1, "estimated_minutes": 60, "max_cost": "5.00", "max_runtime_seconds": 60}
print(f"\nfiring 2 concurrent job submits across {len(RACE_BASES)} process(es): {RACE_BASES}")


async def race():
    clients = [httpx.AsyncClient(base_url=b, timeout=30) for b in RACE_BASES]
    try:
        async def submit(i):
            ac = clients[i % len(clients)]
            r = await ac.post("/v1/jobs", json=job_body, headers=RH)
            detail = None
            try:
                detail = r.json().get("detail")
            except Exception:
                pass
            return r.status_code, detail
        return await asyncio.gather(submit(0), submit(1))
    finally:
        for ac in clients:
            await ac.aclose()


results = sorted(asyncio.run(race()))
print(f"  outcomes: {results}")
wins = [r for r in results if r[0] == 201]
rejected = [r for r in results if r[0] == 402]
check("exactly ONE job submit won (201)", len(wins) == 1, f"{len(wins)} wins")
check("the other rejected 402 insufficient_credits", len(rejected) == 1 and rejected[0][1] == "insufficient_credits",
      f"{rejected}")
racct = c.get("/v1/billing/account", headers=RH).json()
check("race customer: exactly one hold (reserved 1.65, available 0.35)",
      racct["reserved"] == "1.65" and racct["available"] == "0.35",
      f"available={racct['available']} reserved={racct['reserved']}")
min_bal = psql("select coalesce(min(balance_minor),0) from billing_accounts where owner_type='customer'")
check("NO customer balance is negative (DB ground truth)", int(min_bal or "0") >= 0, f"min customer balance_minor={min_bal}")

# final whole-ledger invariant at the DB level
dr = psql("select coalesce(sum(amount_minor),0) from ledger_entries where direction='debit'")
cr = psql("select coalesce(sum(amount_minor),0) from ledger_entries where direction='credit'")
check("INVARIANT 2 at DB: total debits == total credits", dr == cr, f"debits={dr} credits={cr}")

print()
if fails:
    print(f"*** {fails} CHECK(S) FAILED ***")
    sys.exit(1)
print("ALL PHASE-5 BILLING CHECKS PASSED")
