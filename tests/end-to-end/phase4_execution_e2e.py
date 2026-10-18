"""Phase 4 LIVE end-to-end acceptance test (real Postgres, real provider agent).

Proves the job-execution pipeline end to end with NO mocks:
  provider onboards + REAL agent enrolls a machine (synthetic 1-GPU TEST
  capability) -> customer submits a job -> the REAL agent jobrunner claims,
  executes it (SimulatedExecutor), uploads the artifact and posts the result ->
  the job reaches COMPLETED, verification passes, the artifact is downloadable,
  and the FSM event history is correct.

Also proves LEASE RECOVERY: a claimed job whose lease expires (provider
disconnect) is recovered and NOT left stranded in RUNNING.

Run with the provider-agent venv (computecoin_agent + httpx). Requires the API
started with a short LEASE_TTL_SECONDS (e.g. 3) so recovery is quick.
Env: CC_API_BASE (default http://127.0.0.1:8099), COMPUTECOIN_CONFIG_DIR,
OUT_MACHINE_FILE (optional).
"""
import os
import sys
import time
import tempfile
from pathlib import Path

import httpx
from computecoin_agent import enrollment, heartbeat, jobrunner

BASE = os.environ.get("CC_API_BASE", "http://127.0.0.1:8099")
CONFIG_DIR = Path(os.environ.get("COMPUTECOIN_CONFIG_DIR") or tempfile.mkdtemp())
pw = "Str0ngPass-123!"
c = httpx.Client(base_url=BASE, timeout=30)
fails = 0


def check(name, ok, extra=""):
    global fails
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {extra}")
    if not ok:
        fails += 1


def signup(email):
    r = c.post("/auth/register", json={"email": email, "password": pw})
    assert r.status_code in (200, 201), r.text
    vtok = r.json().get("email_verification_token")
    if vtok:
        c.post("/auth/verify-email", json={"token": vtok})
    return c.post("/auth/login", json={"email": email, "password": pw}).json()["access_token"]


pid = os.getpid()

# ---- provider onboard + real agent enroll (synthetic 1-GPU TEST capability) ----
ptok = signup(f"prov_{pid}@example.com")
PH = {"Authorization": f"Bearer {ptok}"}
c.post("/provider/register", json={"display_name": "E2E GPU Co"}, headers=PH)
enr = c.post("/provider/enrollment-tokens", json={"label": "e2e"}, headers=PH).json()["enrollment_token"]
cap = {
    "verification_status": "detected",
    "cpu_model": "e2e-cpu", "cpu_cores": 8, "ram_mb": 16384, "disk_gb": 200,
    "gpus": [{"model": "E2E-Test-GPU", "vram_mb": 24576, "cuda_version": "12.4"}],
    "supported_workloads": ["text-generation", "batch-inference"],
}
res = enrollment.enroll_remote(BASE, enr, cap, config_path=CONFIG_DIR / "agent.toml")
mid, msec = res.machine_id, res.machine_secret
check("real agent enrolled machine", bool(mid and msec), f"machine_id={mid}")
if os.environ.get("OUT_MACHINE_FILE"):
    Path(os.environ["OUT_MACHINE_FILE"]).write_text(str(mid))
r = c.patch(f"/provider/machines/{mid}",
            json={"availability": "online", "asking_price_per_hour": "1.50",
                  "supported_workloads": ["text-generation", "batch-inference"]}, headers=PH)
check("machine priced + online", r.status_code == 200, f"HTTP {r.status_code}")
heartbeat.send_contract_heartbeat(BASE, str(mid), msec, availability="online")

# ---- customer submits a text-generation job ----
ctok = signup(f"cust_{pid}@example.com")
CH = {"Authorization": f"Bearer {ctok}"}
r = c.post("/v1/jobs",
           json={"workload_type": "text-generation", "model": "echo-sim-v1",
                 "input": {"prompt": "Hello ComputeCoin", "max_tokens": 32},
                 "machine_id": str(mid), "gpu_count": 1, "estimated_minutes": 2,
                 "max_cost": "5.00", "max_runtime_seconds": 60,
                 "retry_policy": {"max_retries": 1}},
           headers={**CH, "Idempotency-Key": f"e2e-{pid}-job1"})
check("submit text-generation job", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:120]}")
job = r.json()
jid = job["job_id"]
check("job reached RESERVED after scheduling", job["status"] in ("reserved", "queued", "matched"),
      f"status={job['status']}")
check("match_explanation present", bool(job.get("match_explanation")), str(job.get("match_explanation"))[:80])

# idempotency: same key -> same job
r2 = c.post("/v1/jobs",
            json={"workload_type": "text-generation", "model": "echo-sim-v1",
                  "input": {"prompt": "Hello ComputeCoin", "max_tokens": 32},
                  "machine_id": str(mid), "gpu_count": 1, "estimated_minutes": 2,
                  "max_cost": "5.00", "max_runtime_seconds": 60},
            headers={**CH, "Idempotency-Key": f"e2e-{pid}-job1"})
check("idempotent submit returns SAME job", r2.json().get("job_id") == jid, r2.json().get("job_id"))

# ---- REAL agent runs the job: claim -> execute (simulated) -> upload -> result ----
outcome = jobrunner.run_one(BASE, str(mid), msec)
check("agent run_one claimed+executed a job", outcome.get("claimed") is True, str(outcome)[:100])

# ---- verify completion ----
job = c.get(f"/v1/jobs/{jid}", headers=CH).json()
check("job COMPLETED", job["status"] == "completed", f"status={job['status']}")
check("execution_mode == simulated (honest)", job.get("execution_mode") == "simulated", job.get("execution_mode"))
check("verification recorded valid", (job.get("verification") or {}).get("valid") is True, str(job.get("verification"))[:80])

# FSM event history
events = c.get(f"/v1/jobs/{jid}/events", headers=CH).json()
states = [e["to_state"] for e in (events if isinstance(events, list) else events.get("items", []))]
check("FSM history ends at completed", states and states[-1] == "completed", f"...{states[-3:]}")
check("FSM history includes running + verifying", "running" in states and "verifying" in states, str(states))

# artifact downloadable + contains the SIMULATED output
arts = c.get(f"/v1/jobs/{jid}/artifacts", headers=CH).json()
arts = arts if isinstance(arts, list) else arts.get("items", [])
check("job has an output artifact", len(arts) >= 1, f"{len(arts)} artifact(s)")
if arts:
    aid = arts[0]["artifact_id"]
    dl = c.get(f"/v1/jobs/{jid}/artifacts/{aid}/download", headers=CH).json()
    url = dl.get("url") or dl.get("download_url")
    body = c.get(url).text if url else ""
    check("artifact download returns the simulated output",
          "simulated" in body.lower(), body[:100])
    # non-owner cannot download
    otok = signup(f"other_{pid}@example.com")
    r = c.get(f"/v1/jobs/{jid}/artifacts/{aid}/download",
              headers={"Authorization": f"Bearer {otok}"})
    check("non-owner cannot access artifact (404)", r.status_code == 404, f"HTTP {r.status_code}")

# ---- batch-inference job (second workload) ----
r = c.post("/v1/jobs",
           json={"workload_type": "batch-inference", "model": "echo-sim-v1",
                 "input": {"items": [{"x": 1}, {"x": 2}, {"x": 3}]},
                 "machine_id": str(mid), "gpu_count": 1, "estimated_minutes": 2,
                 "max_cost": "5.00", "max_runtime_seconds": 60},
           headers=CH)
bid = r.json()["job_id"]
jobrunner.run_one(BASE, str(mid), msec)
bjob = c.get(f"/v1/jobs/{bid}", headers=CH).json()
check("batch-inference job COMPLETED", bjob["status"] == "completed", f"status={bjob['status']}")

# ---- LEASE RECOVERY: claim a job, let the lease expire, confirm it is not stranded ----
r = c.post("/v1/jobs",
           json={"workload_type": "text-generation", "model": "echo-sim-v1",
                 "input": {"prompt": "disconnect me", "max_tokens": 8},
                 "machine_id": str(mid), "gpu_count": 1, "estimated_minutes": 2,
                 "max_cost": "5.00", "max_runtime_seconds": 60,
                 "retry_policy": {"max_retries": 1}},
           headers=CH)
rid = r.json()["job_id"]
claimed = jobrunner.claim_job(BASE, str(mid), msec)   # claim only -> RUNNING, then "disconnect" (never post result)
check("job claimed into RUNNING (then we 'disconnect')", bool(claimed), f"claimed job {claimed.get('job_id') if isinstance(claimed, dict) else claimed}")
running = c.get(f"/v1/jobs/{rid}", headers=CH).json()
check("job is RUNNING before lease expiry", running["status"] == "running", f"status={running['status']}")
print("  ...waiting for the lease to expire (provider disconnected)...")
time.sleep(5)   # LEASE_TTL_SECONDS=3 on the server
recovered = c.get(f"/v1/jobs/{rid}", headers=CH).json()  # GET triggers lazy lease recovery
check("disconnected job RECOVERED (not stuck in RUNNING)", recovered["status"] != "running",
      f"status={recovered['status']} attempts_made={recovered.get('attempts_made')}")
check("recovery incremented attempts_made", (recovered.get("attempts_made") or 0) >= 1,
      f"attempts_made={recovered.get('attempts_made')}")

print()
if fails:
    print(f"*** {fails} CHECK(S) FAILED ***")
    sys.exit(1)
print("ALL PHASE-4 EXECUTION CHECKS PASSED")
