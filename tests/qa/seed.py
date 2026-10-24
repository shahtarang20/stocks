"""QA seed: realistic multi-role data through the real API and real provider agent."""
import json, os, subprocess, sys, tempfile, time, uuid
from pathlib import Path
import httpx
from computecoin_agent import enrollment, heartbeat, jobrunner

import os
BASE = os.environ.get("QA_API", "http://localhost:8010")
DSN = os.environ.get("QA_PG", "postgresql://cc@localhost:55432/computecoin")
PW = "Password123!"
OUT = {}

def psql(q):
    return subprocess.run(["psql", DSN, "-tAc", q], capture_output=True, text=True).stdout.strip()

def user(email, roles=()):
    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.post("/auth/register", json={"email": email, "password": PW})
    assert r.status_code == 201, r.text
    c.post("/auth/verify-email", json={"token": r.json()["email_verification_token"]})
    uid = psql(f"select id from users where email='{email}'")
    for role in roles:
        psql(f"insert into user_roles (id,user_id,role) values (gen_random_uuid(),'{uid}','{role}') on conflict do nothing")
    t = c.post("/auth/login", json={"email": email, "password": PW}).json()["access_token"]
    c.headers["Authorization"] = f"Bearer {t}"
    OUT[email] = uid
    return c, uid

def machine(prov, host, gpu, vram, price, region, online=True, workloads=("text-generation", "batch-inference")):
    enr = prov.post("/provider/enrollment-tokens", json={"label": host}).json()["enrollment_token"]
    cap = {"verification_status": "detected", "cpu_model": "EPYC 7542", "cpu_cores": 32, "ram_mb": 131072, "disk_gb": 1000,
           "gpus": [{"model": gpu, "vram_mb": vram, "cuda_version": "12.4"}], "supported_workloads": list(workloads)}
    cfg = Path(tempfile.mkdtemp()) / "agent.toml"
    res = enrollment.enroll_remote(BASE, enr, cap, config_path=cfg, hostname=host)
    mid, msec = str(res.machine_id), res.machine_secret
    prov.patch(f"/provider/machines/{mid}", json={"availability": "online" if online else "offline", "asking_price_per_hour": price,
               "region": region, "supported_workloads": list(workloads)})
    if online:
        heartbeat.send_contract_heartbeat(BASE, mid, msec, availability="online")
    return mid, msec

JOB = lambda mid, prompt, wl="text-generation", **kw: {"workload_type": wl, "model": "echo-sim-v1", "input": ({"items": [{"x": 1}, {"x": 2}]} if wl == "batch-inference" else {"prompt": prompt, "max_tokens": 32}),
        "machine_id": mid, "gpu_count": 1, "estimated_minutes": 2, "max_cost": "5.00", "max_runtime_seconds": 60, **kw}

admin, _ = user("admin@example.com", ["administrator"])
p1, p1id = user("prov1@example.com"); p1.post("/provider/register", json={"display_name": "Alpha GPU Co", "contact_email": "prov1@example.com"})
p2, p2id = user("prov2@example.com"); p2.post("/provider/register", json={"display_name": "Beta Compute", "contact_email": "prov2@example.com"})
c1, c1id = user("cust1@example.com"); c2, c2id = user("cust2@example.com"); c3, c3id = user("empty@example.com")
sus, susid = user("suspended@example.com"); admin.post(f"/v1/admin/users/{susid}/suspend", json={"reason": "qa seed"})

m1, s1 = machine(p1, "alpha-rtx4090", "NVIDIA RTX 4090", 24576, "1.50", "us-east")
m2, s2 = machine(p1, "alpha-a100", "NVIDIA A100", 81920, "3.20", "eu-west")
m3, s3 = machine(p1, "alpha-offline", "NVIDIA RTX 3080", 10240, "0.80", "us-west", online=False)
m4, s4 = machine(p2, "beta-h100", "NVIDIA H100", 81920, "5.00", "ap-south", workloads=("text-generation", "batch-inference", "image-generation"))
OUT.update(machines=[m1, m2, m3, m4], secrets={m1: s1, m2: s2, m3: s3, m4: s4})

# projects + keys
for c, n in ((c1, "alpha-project"), (c1, "beta-project"), (c2, "c2-project")):
    pr = c.post("/projects", json={"name": n, "description": f"{n} seeded"}).json()
    c.post(f"/projects/{pr['project_id'] if 'project_id' in pr else pr['id']}/api-keys", json={"name": f"{n}-key"})

jobs = {}
def submit(c, mid, label, **kw):
    r = c.post("/v1/jobs", json=JOB(mid, label, **kw), headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code in (200, 201), r.text
    jobs[label] = r.json()["job_id"]; return r.json()["job_id"]

# completed (x3 on different machines), run through the real agent
for c, mid, ms, lab in ((c1, m1, s1, "done-1"), (c1, m1, s1, "done-2"), (c2, m2, s2, "done-3")):
    submit(c, mid, lab); print(lab, jobrunner.run_one(BASE, mid, ms).get("status"))
# batch-inference completed
submit(c1, m1, "batch-done", wl="batch-inference"); print("batch", jobrunner.run_one(BASE, m1, s1).get("status"))
# failed: machine declares a workload the executor cannot run
jid = submit(c2, m4, "failed-1", retry_policy={"max_retries": 0})
cl = jobrunner.claim_job(BASE, m4, s4)
jobrunner.post_result(BASE, m4, s4, jid, {"status": "failed", "execution_mode": "simulated", "artifacts": [], "metrics": {"duration_seconds": 1.2},
    "evidence": {"output_schema_valid": False}, "error": {"code": "executor_error", "message": "CUDA out of memory (seeded failure)"}})
print("failed ->", c2.get(f"/v1/jobs/{jid}").json()["status"])
# cancelled while reserved
jid = submit(c1, m2, "cancelled-1"); print("cancel", c1.post(f"/v1/jobs/{jid}/cancel").status_code)
# running (claimed, never finished)
submit(c2, m2, "running-1"); print("claimed", bool(jobrunner.claim_job(BASE, m2, s2)))
# reserved/queued (not claimed)
submit(c1, m1, "reserved-1")
OUT["jobs"] = jobs

# disputes: one open, one resolved
d1 = c1.post(f"/v1/jobs/{jobs['done-1']}/dispute", json={"reason": "Output did not match expectation (open)"}); print("dispute open", d1.status_code)
d2 = c1.post(f"/v1/jobs/{jobs['done-2']}/dispute", json={"reason": "Charged twice, please review (to resolve)"}); print("dispute2", d2.status_code)
if d2.status_code in (200, 201):
    did = d2.json().get("dispute_id") or d2.json().get("id")
    r = admin.post(f"/v1/admin/disputes/{did}/resolve", json={"resolution": "rejected", "note": "seed"}); print("resolve", r.status_code, r.text[:100])

# quotes + reservations
q = c1.post("/v1/quotes", json={"machine_id": m1, "workload_type": "text-generation", "estimated_minutes": 30}); print("quote", q.status_code)
if q.status_code in (200, 201):
    print("reserve", c1.post(f"/v1/quotes/{q.json().get('quote_id') or q.json().get('id')}/reserve").status_code)

# credits + payouts
c1.post("/v1/billing/credits/purchase", json={"amount": "250.00"})
print("payout1", p1.post("/v1/provider/payouts", json={"amount": "0.01"}).status_code)
r = p1.post("/v1/provider/payouts", json={"amount": "0.01"}); print("payout2", r.status_code)
pays = p1.get("/v1/provider/payouts").json(); pays = pays if isinstance(pays, list) else pays.get("items", [])
if pays: print("authorize", admin.post(f"/v1/admin/payouts/{pays[-1].get('payout_id') or pays[-1].get('id')}/authorize").status_code)

# refund
print("refund", admin.post("/v1/admin/refunds", json={"job_id": jobs["done-3"], "amount": "0.01", "reason": "qa seed"}).status_code)
json.dump(OUT, open(os.environ.get("QA_OUT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "seed.json")), "w"), indent=1)
print("SEED DONE")
