"""Deep LIVE journey over the paths the baseline acceptance script does not reach.

full_journey_e2e.py proves the money spine: signup -> provider -> job -> billing
-> admin. This one walks everything ELSE a user can click, because that is where
the bugs were:

  projects, API keys (issue / authenticate / revoke), quotes and reservations,
  artifact upload and download through signed grants, job logs and event
  history, usage, invoice detail and download, the dispute lifecycle end to end,
  provider earnings and payout (including an overdraw attempt), admin
  suspend/reinstate, and the vendor quota cards.

It found two real defects on its first run:
  - API keys authenticated NOTHING. Create, list and revoke all worked, so the
    lifecycle tests passed, but no code path ever consumed a key and every one
    returned 401 -- including on the OpenAI-compatible endpoints the console
    advertises them for.
  - GET /v1/admin/jobs 500'd on any non-empty job list (fixed earlier; its unit
    test ran against an empty table).

Negative controls are interleaved with the happy path on purpose: a forged
upload grant, an overdrawn payout, a revoked key and self-suspension must each
be refused, and a run where they are NOT refused is a failure even though every
positive step passed.

Usage:
    python tests/end-to-end/deep_journey_e2e.py <base_url> <pg_dsn>
"""
import hashlib
import hmac
import json
import subprocess
import sys
import time
import uuid

import httpx

BASE = sys.argv[1]
PGDSN = sys.argv[2]
PW = "Password123!"
RUN = uuid.uuid4().hex[:6]

results = []


def psql(sql):
    o = subprocess.run(["psql", PGDSN, "-tAc", sql], capture_output=True, text=True, timeout=30)
    if o.returncode:
        raise RuntimeError(o.stderr.strip())
    return o.stdout.strip()


def step(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def csrf(c):
    t = c.cookies.get("csrf_token")
    if t:
        c.headers["X-CSRF-Token"] = t


def mk(email, roles=()):
    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.post("/auth/register", json={"email": email, "password": PW})
    tok = (r.json() or {}).get("email_verification_token")
    if tok:
        c.post("/auth/verify-email", json={"token": tok})
    uid = psql(f"select id from users where email='{email}'")
    for role in roles:
        psql(f"insert into user_roles (id,user_id,role) values (gen_random_uuid(),'{uid}','{role}') on conflict do nothing")
    c.post("/auth/login", json={"email": email, "password": PW})
    csrf(c)
    return c, uid


def sign(mid, key, body):
    ts = str(int(time.time()))
    canon = f"{mid}\n{ts}\n{hashlib.sha256(body).hexdigest()}"
    return {
        "X-ComputeCoin-Machine": mid,
        "X-ComputeCoin-Timestamp": ts,
        "X-ComputeCoin-Signature": hmac.new(key.encode(), canon.encode(), hashlib.sha256).hexdigest(),
        "Content-Type": "application/json",
    }


def main():
    print(f"=== DEEP E2E (run {RUN}) ===\n")
    cust, cust_id = mk(f"d-cust-{RUN}@x.com")
    prov, prov_id = mk(f"d-prov-{RUN}@x.com", ["provider"])
    adm, adm_id = mk(f"d-adm-{RUN}@x.com", ["administrator"])

    # ---------------- projects ----------------
    print("\n--- Projects ---")
    r = cust.post("/projects", json={"name": f"proj-{RUN}"})
    step("create project", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:150]}")
    proj = r.json() if r.status_code in (200, 201) else {}
    pid = proj.get("project_id") or proj.get("id")
    r = cust.get("/projects")
    step("list projects shows it", r.status_code == 200 and any(
        str(p.get("project_id") or p.get("id")) == str(pid) for p in (r.json() or [])), f"HTTP {r.status_code}")

    # ---------------- api keys ----------------
    print("\n--- API keys ---")
    r = cust.post(f"/projects/{pid}/api-keys", json={"name": f"key-{RUN}"})
    step("create api key", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:200]}")
    kj = r.json() if r.status_code in (200, 201) else {}
    raw_key = kj.get("api_key") or kj.get("key") or kj.get("secret")
    kid = kj.get("api_key_id") or kj.get("id")
    step("raw key returned exactly once", bool(raw_key), f"keys={list(kj)}")

    if raw_key:
        r = cust.get(f"/projects/{pid}/api-keys")
        body = r.text
        step("raw key NEVER reappears in list", raw_key not in body, "leaked!" if raw_key in body else "")
        # use it
        kc = httpx.Client(base_url=BASE, timeout=30, headers={"Authorization": f"Bearer {raw_key}"})
        r = kc.get("/v1/jobs")
        step("api key authenticates", r.status_code == 200, f"HTTP {r.status_code} {r.text[:120]}")

    if kid:
        r = cust.delete(f"/projects/{pid}/api-keys/{kid}")
        step("revoke api key", r.status_code in (200, 204), f"HTTP {r.status_code}")
        if raw_key:
            kc = httpx.Client(base_url=BASE, timeout=30, headers={"Authorization": f"Bearer {raw_key}"})
            r = kc.get("/v1/jobs")
            step("REVOKED key is rejected", r.status_code in (401, 403), f"HTTP {r.status_code}")

    # ---------------- provider + machine ----------------
    print("\n--- Provider onboarding ---")
    prov.post("/provider/register", json={"display_name": f"Fleet {RUN}", "contact_email": f"d-prov-{RUN}@x.com"})
    r = prov.post("/provider/enrollment-tokens", json={"label": "n1"})
    tok = (r.json() or {}).get("enrollment_token")
    ag = httpx.Client(base_url=BASE, timeout=30)
    r = ag.post("/v1/agents/enroll", json={
        "enrollment_token": tok, "hostname": f"n-{RUN}", "agent_version": "0.1.0",
        "capability_report": {"cpu_cores": 32, "ram_mb": 262144, "disk_gb": 2048,
                              "gpus": [{"model": "NVIDIA A100-SXM4-80GB", "vram_mb": 81920}],
                              "supported_workloads": ["text-generation", "batch-inference"]},
    })
    ej = r.json()
    mid, secret = ej.get("machine_id"), ej.get("machine_secret")
    key = secret[4:] if secret and secret.startswith("msk_") else secret
    step("machine enrolled", bool(mid), f"HTTP {r.status_code}")
    hb = json.dumps({"availability": "online", "capability_summary": {}, "health": {"healthy": True}}).encode()
    r = ag.post("/v1/agents/heartbeat", content=hb, headers=sign(mid, key, hb))
    step("heartbeat ok", r.status_code == 200, f"HTTP {r.status_code}")
    psql(f"update provider_machines set asking_price_per_hour=1.50 where id='{mid}'")

    # ---------------- reservations ----------------
    print("\n--- Reservations ---")
    cust.post("/v1/billing/credits/purchase", json={"amount": "500.00"})
    r = cust.post("/v1/quotes", json={"machine_id": mid, "workload_type": "text-generation",
                                      "estimated_minutes": 5, "gpu_count": 1})
    q = r.json() if r.status_code in (200, 201) else {}
    qid = q.get("quote_id")
    step("quote created", bool(qid), f"HTTP {r.status_code} {r.text[:150]}")
    if qid:
        r = cust.post(f"/v1/quotes/{qid}/reserve", json={})
        step("reserve from quote", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:200]}")
        res = r.json() if r.status_code in (200, 201) else {}
        rid = res.get("reservation_id") or res.get("id")
        r = cust.get("/v1/reservations")
        step("reservation listed", r.status_code == 200 and len(r.json() or []) >= 1, f"HTTP {r.status_code}")
        if rid:
            r = cust.post(f"/v1/reservations/{rid}/release", json={})
            step("release reservation", r.status_code in (200, 204), f"HTTP {r.status_code} {r.text[:150]}")

    # ---------------- job -> artifacts ----------------
    print("\n--- Job, artifacts, logs, events ---")
    r = cust.post("/v1/jobs", json={
        "workload_type": "text-generation", "model": "echo-sim-v1",
        "input": {"prompt": "deep e2e"}, "machine_id": mid,
        "gpu_count": 1, "estimated_minutes": 5, "max_cost": "20.00", "project_id": pid,
    })
    step("job submitted", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:200]}")
    jid = (r.json() or {}).get("job_id")

    claimed = None
    for _ in range(25):
        b = b"{}"
        rr = ag.post("/v1/agents/jobs/claim", content=b, headers=sign(mid, key, b))
        if rr.status_code == 200 and rr.text.strip() not in ("", "null"):
            claimed = rr.json()
            break
        time.sleep(2)
    step("agent claimed", claimed is not None, "")

    art_id = None
    if claimed:
        grant = claimed.get("artifact_upload") or {}
        url, atok = grant.get("url"), grant.get("token")
        if url:
            up = url if url.startswith("http") else BASE + url
            payload = b"deep-e2e artifact payload"
            rr = httpx.put(up, content=payload, timeout=30,
                           headers={"Content-Type": "application/octet-stream"})
            step("artifact upload via signed grant", rr.status_code in (200, 201),
                 f"HTTP {rr.status_code} {rr.text[:150]}")
            if rr.status_code in (200, 201):
                art_id = (rr.json() or {}).get("artifact_id")
            # forged grant must fail
            bad = up.rsplit("/", 1)[0] + "/" + ("A" * len(atok or "x"))
            rr = httpx.put(bad, content=payload, timeout=30)
            step("FORGED upload grant rejected", rr.status_code in (401, 403, 404),
                 f"HTTP {rr.status_code}")

        logs = json.dumps({"lines": ["deep e2e line 1", "line 2"]}).encode()
        rr = ag.post(f"/v1/agents/jobs/{jid}/logs", content=logs, headers=sign(mid, key, logs))
        step("agent posted logs", rr.status_code in (200, 201), f"HTTP {rr.status_code} {rr.text[:120]}")

        body = {"status": "success", "execution_mode": "simulated",
                "output": {"text": "echo: deep e2e"},
                "metrics": {"duration_seconds": 120}}
        if art_id:
            body["artifacts"] = [{"artifact_id": art_id, "kind": "output"}]
        rb = json.dumps(body).encode()
        rr = ag.post(f"/v1/agents/jobs/{jid}/result", content=rb, headers=sign(mid, key, rb))
        step("agent posted result", rr.status_code in (200, 201), f"HTTP {rr.status_code} {rr.text[:200]}")

    final = {}
    for _ in range(20):
        r = cust.get(f"/v1/jobs/{jid}")
        final = r.json() if r.status_code == 200 else {}
        if (final.get("status") or "").lower() in ("completed", "failed"):
            break
        time.sleep(2)
    step("job completed", (final.get("status") or "").lower() == "completed", f"status={final.get('status')}")

    r = cust.get(f"/v1/jobs/{jid}/events")
    step("job events serialize", r.status_code == 200 and len(r.json() or []) >= 1,
         f"HTTP {r.status_code} n={len(r.json() or []) if r.status_code==200 else '-'}")
    r = cust.get(f"/v1/jobs/{jid}/logs")
    step("job logs readable", r.status_code == 200, f"HTTP {r.status_code}")
    r = cust.get(f"/v1/jobs/{jid}/artifacts")
    arts = r.json() if r.status_code == 200 else []
    step("artifacts listed", r.status_code == 200, f"HTTP {r.status_code} n={len(arts)}")
    if arts:
        aid = arts[0].get("artifact_id")
        r = cust.get(f"/v1/jobs/{jid}/artifacts/{aid}/download")
        step("artifact download grant issued", r.status_code == 200, f"HTTP {r.status_code} {r.text[:120]}")
        du = (r.json() or {}).get("url")
        if du:
            full = du if du.startswith("http") else BASE + du
            rr = httpx.get(full, timeout=30)
            step("artifact content downloads", rr.status_code == 200 and b"deep-e2e" in rr.content,
                 f"HTTP {rr.status_code} {len(rr.content)}b")

    # ---------------- usage + invoice ----------------
    print("\n--- Usage & invoices ---")
    r = cust.get("/v1/billing/usage")
    if r.status_code == 404:
        r = cust.get("/v1/usage")
    step("usage endpoint", r.status_code == 200, f"HTTP {r.status_code} {r.text[:120]}")
    r = cust.get("/v1/invoices")
    invs = r.json() if r.status_code == 200 else []
    step("invoice created for the job", len(invs) >= 1, f"n={len(invs)}")
    if invs:
        iid = invs[0].get("invoice_id") or invs[0].get("id")
        r = cust.get(f"/v1/invoices/{iid}")
        step("invoice detail", r.status_code == 200, f"HTTP {r.status_code}")
        r = cust.get(f"/v1/invoices/{iid}/download")
        step("invoice download", r.status_code in (200, 302, 307), f"HTTP {r.status_code} {r.text[:120]}")

    # ---------------- dispute ----------------
    print("\n--- Dispute lifecycle ---")
    r = cust.post(f"/v1/jobs/{jid}/dispute", json={"reason": "output_incorrect",
                                                   "description": "deep e2e dispute"})
    step("customer opens dispute", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:200]}")
    did = (r.json() or {}).get("dispute_id") or (r.json() or {}).get("id")
    if did:
        r = adm.get(f"/v1/admin/disputes/{did}/evidence")
        step("admin sees auto-attached evidence", r.status_code == 200, f"HTTP {r.status_code} {r.text[:150]}")
        r = adm.post(f"/v1/admin/disputes/{did}/adjudicate",
                     json={"resolution": "upheld", "refund_amount": "0.01", "reason": "e2e"})
        step("admin adjudicates", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:200]}")

    # ---------------- provider earnings + payout ----------------
    print("\n--- Provider earnings & payout ---")
    r = prov.get("/v1/provider/earnings")
    step("provider earnings readable", r.status_code == 200, f"HTTP {r.status_code} {r.text[:160]}")
    earn = r.json() if r.status_code == 200 else {}
    r = prov.post("/v1/provider/payouts", json={"amount": "0.01"})
    step("provider requests payout", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:200]}")
    pid2 = (r.json() or {}).get("payout_id") or (r.json() or {}).get("id")
    r = prov.post("/v1/provider/payouts", json={"amount": "999999.00"})
    step("OVERDRAW payout rejected", r.status_code >= 400, f"HTTP {r.status_code} {r.text[:120]}")
    if pid2:
        r = adm.post(f"/v1/admin/payouts/{pid2}/authorize", json={})
        step("admin authorizes payout", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:200]}")

    # ---------------- admin user mgmt ----------------
    print("\n--- Admin user management ---")
    victim, vid = mk(f"d-victim-{RUN}@x.com")
    r = adm.post(f"/v1/admin/users/{vid}/suspend", json={})
    step("admin suspends user", r.status_code == 200, f"HTTP {r.status_code} {r.text[:150]}")
    r = victim.get("/v1/jobs")
    step("SUSPENDED user is blocked", r.status_code == 401, f"HTTP {r.status_code}")
    r = adm.post(f"/v1/admin/users/{vid}/reinstate", json={})
    step("admin reinstates", r.status_code == 200, f"HTTP {r.status_code}")
    r = adm.post(f"/v1/admin/users/{adm_id}/suspend", json={})
    step("admin CANNOT suspend self", r.status_code == 400, f"HTTP {r.status_code} {r.text[:120]}")

    # ---------------- vendors ----------------
    print("\n--- Admin vendors ---")
    r = adm.get("/v1/admin/vendors")
    step("vendor cards load", r.status_code == 200 and len(r.json() or []) == 9,
         f"HTTP {r.status_code} n={len(r.json() or []) if r.status_code==200 else '-'}")
    r = adm.get("/v1/admin/vendors/summary")
    step("vendor summary", r.status_code == 200, f"HTTP {r.status_code}")

    # ---------------- final integrity ----------------
    print("\n--- Final money integrity ---")
    imb = psql("select coalesce(sum(case when direction='debit' then amount_minor else -amount_minor end),0) from ledger_entries")
    step("ledger still balanced", imb == "0", f"imbalance={imb}")
    neg = psql("select count(*) from billing_accounts where balance_minor<0 and type in ('customer_credits','holding','provider_earnings')")
    step("no negative guarded accounts", neg == "0", f"n={neg}")

    print("\n" + "=" * 64)
    bad = [(n, d) for n, ok, d in results if not ok]
    print(f"{len(results) - len(bad)}/{len(results)} passed")
    if bad:
        print("\nFAILURES:")
        for n, d in bad:
            print(f"  - {n}: {d}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
