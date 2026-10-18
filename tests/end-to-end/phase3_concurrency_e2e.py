"""Phase 3 LIVE concurrency acceptance test (real Postgres, multi-worker API).

Proves the core invariant: a machine with max_concurrent_jobs=1 can never be
double-booked, even under simultaneous reservation requests spread across
multiple API worker PROCESSES (where an in-process lock cannot help and only the
Postgres `SELECT ... FOR UPDATE` row lock serializes the check-and-insert).

Flow:
  provider register -> REAL agent enroll (synthetic 1-GPU TEST capability) ->
  set price + online -> signed heartbeat -> customer creates N quotes ->
  fire N /reserve concurrently -> assert EXACTLY ONE 201 and N-1 '409 no_capacity'.

Env:
  CC_API_BASE   (default http://127.0.0.1:8099)
  COMPUTECOIN_CONFIG_DIR  (temp dir for the agent credential)
  OUT_MACHINE_FILE (optional path to write the machine_id for external DB checks)

Run with the provider-agent venv (has computecoin_agent + httpx).
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import httpx
from computecoin_agent import enrollment, heartbeat

BASE = os.environ.get("CC_API_BASE", "http://127.0.0.1:8099")
CONFIG_DIR = Path(os.environ.get("COMPUTECOIN_CONFIG_DIR") or tempfile.mkdtemp())
N = int(os.environ.get("CC_CONCURRENCY", "12"))
pw = "Str0ngPass-123!"
fails = 0


def check(name, ok, extra=""):
    global fails
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {extra}")
    if not ok:
        fails += 1


def signup(c, email):
    r = c.post("/auth/register", json={"email": email, "password": pw})
    assert r.status_code in (200, 201), r.text
    vtok = r.json().get("email_verification_token")
    if vtok:
        c.post("/auth/verify-email", json={"token": vtok})
    r = c.post("/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


pid = os.getpid()
c = httpx.Client(base_url=BASE, timeout=30)

# --- provider onboard + real agent enroll with a synthetic 1-GPU TEST capability ---
ptoken = signup(c, f"prov_{pid}@example.com")
PH = {"Authorization": f"Bearer {ptoken}"}
c.post("/provider/register", json={"display_name": "E2E GPU Co"}, headers=PH)
enr = c.post("/provider/enrollment-tokens", json={"label": "e2e"}, headers=PH).json()["enrollment_token"]

test_capability = {
    "verification_status": "detected",          # agent never claims verified
    "cpu_model": "e2e-test-cpu", "cpu_cores": 4, "ram_mb": 8192, "disk_gb": 100,
    "gpus": [{"model": "E2E-Test-GPU", "vram_mb": 16384, "cuda_version": "12.4"}],
    "supported_workloads": ["text-generation", "batch-inference"],
}
res = enrollment.enroll_remote(BASE, enr, test_capability, config_path=CONFIG_DIR / "agent.toml")
mid, msec = res.machine_id, res.machine_secret
check("real agent enrolled test machine", bool(mid and msec), f"machine_id={mid}")
if os.environ.get("OUT_MACHINE_FILE"):
    Path(os.environ["OUT_MACHINE_FILE"]).write_text(str(mid))

# price it + bring online (provider management), then a signed heartbeat to set last_heartbeat_at
r = c.patch(f"/provider/machines/{mid}",
            json={"availability": "online", "asking_price_per_hour": "1.50",
                  "supported_workloads": ["text-generation", "batch-inference"]},
            headers=PH)
check("machine priced + set online (PATCH)", r.status_code == 200, f"HTTP {r.status_code}")
heartbeat.send_contract_heartbeat(BASE, str(mid), msec, availability="online")

# machine is listable in the public marketplace
r = c.get("/v1/marketplace/machines", params={"workload": "text-generation"})
listed = [m for m in r.json()["items"] if m["machine_id"] == str(mid)]
check("machine listable in marketplace", len(listed) == 1,
      f"available_slots={listed[0]['available_slots'] if listed else 'n/a'} "
      f"max={listed[0]['max_concurrent_jobs'] if listed else 'n/a'}")
check("machine advertises exactly 1 slot", listed and listed[0]["max_concurrent_jobs"] == 1)

# --- customer creates N quotes on the single-slot machine ---
ctoken = signup(c, f"cust_{pid}@example.com")
CH = {"Authorization": f"Bearer {ctoken}"}
quote_ids = []
for _ in range(N):
    r = c.post("/v1/quotes",
               json={"machine_id": str(mid), "workload_type": "text-generation",
                     "estimated_minutes": 60, "gpu_count": 1},
               headers=CH)
    assert r.status_code == 201, r.text
    quote_ids.append(r.json()["quote_id"])
check(f"customer created {N} quotes", len(quote_ids) == N)
# verify itemized pricing on one quote (60min @1.50, fee 0.10 -> total 1.65)
b = c.get(f"/v1/quotes/{quote_ids[0]}", headers=CH).json()["breakdown"]
check("quote itemization correct (gpu_cost 1.50, fee 0.15, total 1.65)",
      (b["gpu_cost"], b["platform_fee"], b["total"]) == ("1.50", "0.15", "1.65"),
      f"{b['gpu_cost']}/{b['platform_fee']}/{b['total']}")


# Spread the reservation storm across one or more INDEPENDENT API processes
# (comma-separated in CC_RESERVE_BASES). Each process has its own in-process
# lock, so when there are >= 2 of them only the Postgres row lock can serialize
# the check-and-insert -- a true cross-process proof.
RESERVE_BASES = [b.strip() for b in os.environ.get("CC_RESERVE_BASES", BASE).split(",") if b.strip()]
print(f"\nfiring {N} concurrent reserves across {len(RESERVE_BASES)} API process(es): {RESERVE_BASES}")


async def fire_all():
    clients = [httpx.AsyncClient(base_url=b, timeout=30) for b in RESERVE_BASES]
    try:
        async def reserve(i, qid):
            ac = clients[i % len(clients)]  # round-robin across processes
            r = await ac.post(f"/v1/quotes/{qid}/reserve", json={}, headers=CH)
            return r.status_code, (r.json().get("detail") if r.status_code != 201 else "ok")
        return await asyncio.gather(*[reserve(i, q) for i, q in enumerate(quote_ids)])
    finally:
        for ac in clients:
            await ac.aclose()


results = asyncio.run(fire_all())
codes = sorted(r[0] for r in results)
wins = [r for r in results if r[0] == 201]
conflicts = [r for r in results if r[0] == 409]
print(f"\nconcurrent /reserve outcomes: {sorted((r[0], r[1]) for r in results)}")
check("EXACTLY ONE reservation won (201)", len(wins) == 1, f"{len(wins)} wins")
check(f"the other {N-1} were rejected 409 no_capacity",
      len(conflicts) == N - 1 and all(r[1] == "no_capacity" for r in conflicts),
      f"{len(conflicts)} conflicts")
check("no HTTP 500s / no torn state", set(codes) <= {201, 409}, f"codes={set(codes)}")

# marketplace now shows the slot consumed
r = c.get(f"/v1/marketplace/machines/{mid}")
avail = r.json().get("available_slots") if r.status_code == 200 else None
check("marketplace shows available_slots == 0 after the race", avail == 0, f"available_slots={avail}")

print()
if fails:
    print(f"*** {fails} CHECK(S) FAILED ***")
    sys.exit(1)
print("ALL PHASE-3 CONCURRENCY CHECKS PASSED")
