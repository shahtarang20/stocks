"""LIVE full-journey acceptance test across all four actors (real Postgres, real HTTP).

Walks the whole product the way a real user does, with NO mocks and no test
client: customer signup -> email verification -> credits -> provider onboarding
-> agent enrollment -> HMAC heartbeat -> marketplace listing -> quote -> job
submit -> agent claim -> result -> billing -> invoices -> admin oversight.

Alongside the happy path it asserts the controls actually hold:
  - CSRF double-submit is enforced on state-changing POSTs
  - a forged HMAC signature and a tampered body are both rejected
  - an unregistered model is refused (no silent fallback)
  - a stranger cannot read another tenant's job
  - an unsigned agent call is rejected
  - ledger debits == credits and no guarded account goes negative

Found in practice: GET /v1/admin/jobs raised AttributeError on every row
because the serializer read a non-existent `final_cost_minor`. The unit test
for that endpoint passed because it ran against an EMPTY job table, so the
per-row code never executed. A journey that actually creates a job catches it.

Two steps are marked ADMIN-SETUP and use SQL to stand in for things outside
HTTP: granting roles (a human operator). Email verification goes through the
real /auth/verify-email endpoint using the token the dev-mode API returns.

Usage:
    python tests/end-to-end/full_journey_e2e.py <base_url> <pg_dsn>

e.g. python tests/end-to-end/full_journey_e2e.py \
        http://127.0.0.1:8001 postgresql://ccdev@127.0.0.1:5545/computecoin

Requires: a running API, its Postgres reachable via psql, and the `echo-sim-v1`
model version seeded by the migrations.
"""
import hashlib
import hmac
import json
import subprocess
import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001"
PGDSN = sys.argv[2] if len(sys.argv) > 2 else "postgresql://ccdev@127.0.0.1:5545/computecoin"

PASSWORD = "Password123!"
RUN = uuid.uuid4().hex[:8]
failures: list[str] = []
notes: list[str] = []


def psql(sql: str) -> str:
    out = subprocess.run(
        ["psql", PGDSN, "-tAc", sql], capture_output=True, text=True, timeout=30
    )
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr.strip()}")
    return out.stdout.strip()


def step(name: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(f"{name}: {detail}")
    return ok


def note(msg: str) -> None:
    print(f"[NOTE] {msg}")
    notes.append(msg)


def csrf(cl: httpx.Client) -> None:
    """Mirror the csrf_token cookie into the X-CSRF-Token header (double-submit).

    This is exactly what the browser client does; a request without it is
    correctly refused with 403.
    """
    tok = cl.cookies.get("csrf_token")
    if tok:
        cl.headers["X-CSRF-Token"] = tok


def sign(machine_id: str, signing_key: str, body: bytes) -> dict:
    ts = str(int(time.time()))
    canonical = f"{machine_id}\n{ts}\n{hashlib.sha256(body).hexdigest()}"
    sig = hmac.new(signing_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {
        "X-ComputeCoin-Machine": machine_id,
        "X-ComputeCoin-Timestamp": ts,
        "X-ComputeCoin-Signature": sig,
        "Content-Type": "application/json",
    }


def main() -> int:
    print(f"=== ComputeCoin E2E journey (run {RUN}) against {BASE} ===\n")

    cust_email = f"cust-{RUN}@example.com"
    prov_email = f"prov-{RUN}@example.com"
    admin_email = f"admin-{RUN}@example.com"

    cust = httpx.Client(base_url=BASE, timeout=30)
    prov = httpx.Client(base_url=BASE, timeout=30)
    adm = httpx.Client(base_url=BASE, timeout=30)
    agent = httpx.Client(base_url=BASE, timeout=30)

    # ---------------- 1. signup ----------------
    print("--- 1. Signup & auth ---")
    for cl, email in ((cust, cust_email), (prov, prov_email), (adm, admin_email)):
        r = cl.post("/auth/register", json={"email": email, "password": PASSWORD})
        if not step(f"register {email}", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:120]}"):
            return 1
        # Verify through the real endpoint using the token the dev-mode API
        # hands back (in production this arrives by email instead).
        tok = (r.json() or {}).get("email_verification_token")
        if tok:
            v = cl.post("/auth/verify-email", json={"token": tok})
            step(f"verify email {email}", v.status_code in (200, 204), f"HTTP {v.status_code} {v.text[:120]}")
        else:
            step(f"verify email {email}", False, "no token returned by register")

    # ADMIN-SETUP: stands in for a human operator granting roles.
    uid_prov = psql(f"select id from users where email='{prov_email}'")
    uid_adm = psql(f"select id from users where email='{admin_email}'")
    psql(f"insert into user_roles (id, user_id, role) values (gen_random_uuid(),'{uid_prov}','provider') on conflict do nothing")
    psql(f"insert into user_roles (id, user_id, role) values (gen_random_uuid(),'{uid_adm}','administrator') on conflict do nothing")

    for cl, email in ((cust, cust_email), (prov, prov_email), (adm, admin_email)):
        r = cl.post("/auth/login", json={"email": email, "password": PASSWORD})
        if not step(f"login {email}", r.status_code == 200, f"HTTP {r.status_code} {r.text[:120]}"):
            return 1
    for cl in (cust, prov, adm):
        csrf(cl)
    r = adm.get("/auth/me")
    step("admin has administrator role", "administrator" in r.json().get("roles", []), r.text[:120])

    # CSRF is a real control: prove a state-changing POST without the header fails.
    nocsrf = httpx.Client(base_url=BASE, timeout=30, cookies=cust.cookies)
    r = nocsrf.post("/v1/billing/credits/purchase", json={"amount": "1.00"})
    step("state-changing POST without CSRF header REJECTED", r.status_code == 403, f"HTTP {r.status_code}")

    # ---------------- 2. access control ----------------
    print("\n--- 2. Access control (negative tests) ---")
    r = cust.get("/v1/admin/reconciliation")
    step("customer BLOCKED from admin reconciliation", r.status_code in (401, 403, 404), f"HTTP {r.status_code}")
    r = cust.get("/v1/admin/audit-logs")
    step("customer BLOCKED from audit logs", r.status_code in (401, 403, 404), f"HTTP {r.status_code}")
    anon = httpx.Client(base_url=BASE, timeout=30)
    r = anon.get("/v1/jobs")
    step("anonymous BLOCKED from jobs", r.status_code in (401, 403), f"HTTP {r.status_code}")

    # ---------------- 3. credits ----------------
    print("\n--- 3. Customer buys credits ---")
    r = cust.post("/v1/billing/credits/purchase", json={"amount": "500.00"})
    step("purchase credits", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:160]}")
    r = cust.get("/v1/billing/account")
    bal = r.json() if r.status_code == 200 else {}
    step("balance reflects purchase", r.status_code == 200, f"HTTP {r.status_code} {json.dumps(bal)[:160]}")
    print(f"       balance: {json.dumps(bal)[:200]}")

    # ---------------- 4. provider onboarding ----------------
    print("\n--- 4. Provider onboarding ---")
    r = prov.post("/provider/register", json={"display_name": f"E2E Fleet {RUN}", "contact_email": prov_email})
    step("provider register", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:160]}")
    r = prov.post("/provider/enrollment-tokens", json={"label": "e2e-node"})
    step("create enrollment token", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:160]}")
    tok = (r.json() or {}).get("token") or (r.json() or {}).get("enrollment_token")
    if not tok:
        step("enrollment token present", False, f"body={r.text[:200]}")
        return 1

    enroll_body = {
        "enrollment_token": tok,
        "agent_version": "0.1.0",
        "hostname": f"e2e-node-{RUN}",
        "capability_report": {
            "verification_status": "detected",
            "cpu_model": "AMD EPYC 7763",
            "cpu_cores": 32,
            "ram_mb": 262144,
            "disk_gb": 2048,
            "gpus": [
                {"model": "NVIDIA A100-SXM4-80GB", "vram_mb": 81920, "cuda_version": "12.4"},
                {"model": "NVIDIA A100-SXM4-80GB", "vram_mb": 81920, "cuda_version": "12.4"},
            ],
            "supported_workloads": ["text-generation", "batch-inference"],
            "isolation_tiers": ["container"],
        },
    }
    r = agent.post("/v1/agents/enroll", json=enroll_body)
    step("agent enroll", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:200]}")
    ej = r.json() if r.status_code in (200, 201) else {}
    machine_id = ej.get("machine_id")
    machine_secret = ej.get("machine_secret")
    if not (machine_id and machine_secret):
        step("agent credentials returned", False, f"body={r.text[:250]}")
        return 1
    signing_key = machine_secret[4:] if machine_secret.startswith("msk_") else machine_secret
    print(f"       machine_id={machine_id}")

    # ---------------- 5. HMAC auth ----------------
    print("\n--- 5. Agent HMAC authentication ---")
    hb = json.dumps({
        "availability": "online",
        "capability_summary": {"gpu_count": 2, "gpu_model": "NVIDIA A100-SXM4-80GB", "vram_mb": 81920},
        "health": {"healthy": True},
    }).encode()
    r = agent.post("/v1/agents/heartbeat", content=hb, headers=sign(machine_id, signing_key, hb))
    step("signed heartbeat accepted", r.status_code in (200, 204), f"HTTP {r.status_code} {r.text[:160]}")

    bad = sign(machine_id, "wrong-key-entirely", hb)
    r = agent.post("/v1/agents/heartbeat", content=hb, headers=bad)
    step("FORGED signature REJECTED", r.status_code in (401, 403), f"HTTP {r.status_code}")

    good = sign(machine_id, signing_key, hb)
    tampered = json.dumps({"availability": "online", "evil": True}).encode()
    r = agent.post("/v1/agents/heartbeat", content=tampered, headers=good)
    step("TAMPERED body REJECTED", r.status_code in (401, 403), f"HTTP {r.status_code}")

    # ---------------- 6. marketplace ----------------
    print("\n--- 6. Marketplace & quote ---")
    psql(f"update provider_machines set availability='online', asking_price_per_hour=1.50 where id='{machine_id}'")
    r = cust.get("/v1/marketplace/machines")
    items = r.json() if r.status_code == 200 else []
    if isinstance(items, dict):
        items = items.get("items") or items.get("machines") or []
    found = any(str(m.get("machine_id") or m.get("id")) == machine_id for m in items)
    step("machine listed in marketplace", found, f"HTTP {r.status_code}, {len(items)} machines")

    r = cust.post("/v1/quotes", json={
        "machine_id": machine_id, "workload_type": "text-generation",
        "estimated_minutes": 10, "gpu_count": 1,
    })
    step("quote created", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:200]}")
    q = r.json() if r.status_code in (200, 201) else {}
    print(f"       quote: {json.dumps(q)[:240]}")

    # ---------------- 7. job ----------------
    print("\n--- 7. Job submission & execution ---")
    r = cust.post("/v1/jobs", json={
        "workload_type": "text-generation", "model": "echo-sim-v1",
        "input": {"prompt": "hello from the computecoin e2e journey", "max_tokens": 32},
        "machine_id": machine_id, "gpu_count": 1, "estimated_minutes": 10,
        "max_cost": "25.00",
    })
    step("job submitted", r.status_code in (200, 201, 202), f"HTTP {r.status_code} {r.text[:240]}")
    job = r.json() if r.status_code in (200, 201, 202) else {}
    job_id = job.get("job_id") or job.get("id")
    print(f"       job_id={job_id} status={job.get('status')}")
    if not job_id:
        return 1

    rbad = cust.post("/v1/jobs", json={
        "workload_type": "text-generation", "model": "totally-not-registered-model",
        "input": {"prompt": "x"}, "gpu_count": 1, "estimated_minutes": 1,
    })
    step("UNREGISTERED model REJECTED", rbad.status_code in (400, 422),
         f"HTTP {rbad.status_code} {rbad.text[:120]}")

    claimed = None
    for attempt in range(25):
        body = b"{}"
        r = agent.post("/v1/agents/jobs/claim", content=body, headers=sign(machine_id, signing_key, body))
        if r.status_code == 200 and r.text.strip() not in ("", "null"):
            claimed = r.json()
            break
        time.sleep(2)
    step("agent claimed the job", claimed is not None, f"after polling; last HTTP {r.status_code} {r.text[:160]}")
    if claimed:
        print(f"       claimed job {claimed.get('job_id')} mode={claimed.get('execution_mode')}")

    if claimed:
        rb = json.dumps({
            "status": "success", "execution_mode": "simulated",
            "output": {"text": "echo: hello from the computecoin e2e journey"},
            "metrics": {"duration_seconds": 120, "gpu_seconds": 120},
        }).encode()
        r = agent.post(f"/v1/agents/jobs/{job_id}/result", content=rb,
                       headers=sign(machine_id, signing_key, rb))
        step("agent submitted result", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:200]}")

    final = {}
    for _ in range(20):
        r = cust.get(f"/v1/jobs/{job_id}")
        final = r.json() if r.status_code == 200 else {}
        if (final.get("status") or "").upper() in ("COMPLETED", "FAILED", "VERIFYING", "DISPUTED"):
            break
        time.sleep(2)
    step("job reached COMPLETED", (final.get("status") or "").upper() == "COMPLETED",
         f"status={final.get('status')} err={final.get('error_code')}")
    print(f"       final_cost={final.get('final_cost')} mode={final.get('execution_mode')}")
    step("completed job is billed > 0", (final.get("final_cost") or "0") not in ("0", "0.00", None, ""),
         f"final_cost={final.get('final_cost')}")
    step("execution_mode honestly stamped", final.get("execution_mode") in ("simulated", "docker", "real"),
         f"mode={final.get('execution_mode')}")

    # ---------------- 8. money ----------------
    print("\n--- 8. Billing & ledger integrity ---")
    r = cust.get("/v1/billing/ledger")
    step("customer can read own ledger", r.status_code == 200, f"HTTP {r.status_code}")
    imbalance = psql("select coalesce(sum(case when direction='debit' then amount_minor else -amount_minor end),0) from ledger_entries")
    step("ledger balanced (sum debits == sum credits)", imbalance == "0", f"imbalance_minor={imbalance}")
    neg = psql("select count(*) from billing_accounts where balance_minor < 0 and type in ('customer_credits','holding','provider_earnings')")
    step("no negative balances on guarded accounts", neg == "0", f"negative_accounts={neg}")

    r = cust.get("/v1/invoices")
    step("invoices endpoint reachable", r.status_code == 200, f"HTTP {r.status_code}")

    # ---------------- 9. isolation ----------------
    print("\n--- 9. Cross-tenant isolation ---")
    other = httpx.Client(base_url=BASE, timeout=30)
    oe = f"other-{RUN}@example.com"
    ro = other.post("/auth/register", json={"email": oe, "password": PASSWORD})
    otok = (ro.json() or {}).get("email_verification_token")
    if otok:
        other.post("/auth/verify-email", json={"token": otok})
    other.post("/auth/login", json={"email": oe, "password": PASSWORD})
    csrf(other)
    r = other.get(f"/v1/jobs/{job_id}")
    step("STRANGER cannot read another user's job", r.status_code in (403, 404), f"HTTP {r.status_code}")

    other_body = b"{}"
    r = other.post(f"/v1/agents/jobs/{job_id}/result", content=other_body,
                   headers={"Content-Type": "application/json"})
    step("unsigned agent call REJECTED", r.status_code in (401, 403), f"HTTP {r.status_code}")

    # ---------------- 10. admin ----------------
    print("\n--- 10. Admin oversight ---")
    r = adm.get("/v1/admin/reconciliation")
    step("admin reconciliation", r.status_code == 200, f"HTTP {r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        print(f"       {json.dumps(r.json())[:260]}")
    r = adm.get("/v1/admin/audit-logs")
    step("admin audit log", r.status_code == 200, f"HTTP {r.status_code}")
    r = adm.get("/v1/admin/jobs")
    step("admin can list jobs", r.status_code == 200, f"HTTP {r.status_code}")

    print("\n" + "=" * 62)
    if failures:
        print(f"RESULT: {len(failures)} FAILURE(S)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("RESULT: all journey steps passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
