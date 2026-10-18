"""Phase 2 end-to-end acceptance test (LIVE, no mocks).

Drives a running ComputeCoin API with real HTTP and uses the REAL provider-agent
code (`enroll_remote` + `send_contract_heartbeat`) to enroll a machine and send a
signed heartbeat, then asserts the machine shows ONLINE via the management API.

This is the test that caught the HMAC signing-key contract drift between the
backend and the agent during the Phase 2 build.

Prereqs:
  - A running API (default http://127.0.0.1:8099), e.g.:
      cd apps/api
      DATABASE_URL="sqlite+aiosqlite:///./e2e.db" ENVIRONMENT=development \
        AUTH_SECRET=$(openssl rand -hex 32) SESSION_SECRET=$(openssl rand -hex 32) \
        .venv/bin/alembic upgrade head
      DATABASE_URL="sqlite+aiosqlite:///./e2e.db" ENVIRONMENT=development \
        AUTH_SECRET=... SESSION_SECRET=... \
        .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8099
  - Run with the provider-agent venv (it has `computecoin_agent` + httpx):
      COMPUTECOIN_CONFIG_DIR=$(mktemp -d) CC_API_BASE=http://127.0.0.1:8099 \
        provider-agent/.venv/bin/python -I tests/end-to-end/phase2_provider_e2e.py

Exits non-zero on any failed check.
"""
import os
import sys
import tempfile
from pathlib import Path

import httpx
from computecoin_agent.hardware import build_capability_report
from computecoin_agent import enrollment, heartbeat

BASE = os.environ.get("CC_API_BASE", "http://127.0.0.1:8099")
CONFIG_DIR = Path(os.environ.get("COMPUTECOIN_CONFIG_DIR") or tempfile.mkdtemp())
email = f"prov_{os.getpid()}@example.com"
pw = "Str0ngPass-123!"
c = httpx.Client(base_url=BASE, timeout=20)
fails = 0


def step(name, ok, extra=""):
    global fails
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {extra}")
    if not ok:
        fails += 1


# 1 register (dev mode returns the verification token)
r = c.post("/auth/register", json={"email": email, "password": pw})
step("register", r.status_code in (200, 201), f"HTTP {r.status_code}")
vtok = r.json().get("email_verification_token")
if vtok:
    r = c.post("/auth/verify-email", json={"token": vtok})
    step("verify-email", r.status_code == 200, f"HTTP {r.status_code}")
else:
    step("verify-email", True, "Production mode (token delivered via email)")

# 3 login
r = c.post("/auth/login", json={"email": email, "password": pw})
step("login -> access token", r.status_code == 200, f"HTTP {r.status_code}")
access = r.json()["access_token"]
H = {"Authorization": f"Bearer {access}"}

# 4 become a provider
r = c.post("/provider/register", json={"display_name": "E2E GPU Co"}, headers=H)
step("provider register", r.status_code in (200, 201), f"HTTP {r.status_code}")

# 5 mint enrollment token (shown once)
r = c.post("/provider/enrollment-tokens", json={"label": "e2e"}, headers=H)
step("mint enrollment token", r.status_code in (200, 201), f"HTTP {r.status_code}")
enr = r.json()["enrollment_token"]
step("enrollment token has enr_ prefix", enr.startswith("enr_"), enr[:12] + "...")

# 6 REAL AGENT enroll against the live backend
cap = build_capability_report()
cfg = CONFIG_DIR / "agent.toml"
res = enrollment.enroll_remote(BASE, enr, cap, config_path=cfg)
mid, msec = res.machine_id, res.machine_secret
step("agent enroll_remote -> credential issued", bool(mid and msec), f"machine_id={mid}")
step("machine_secret has msk_ prefix", msec.startswith("msk_"), msec[:12] + "...")
step("agent config persisted 0600", oct(cfg.stat().st_mode & 0o777) == "0o600")

# 7 reusing the same enrollment token must fail (single-use)
try:
    enrollment.enroll_remote(BASE, enr, cap, config_path=CONFIG_DIR / "x.toml")
    step("enrollment token single-use (reuse rejected)", False, "reuse ACCEPTED!")
except Exception as e:
    step("enrollment token single-use (reuse rejected)", True, type(e).__name__)

# 8 machine listed, capability DETECTED, not online before any heartbeat
r = c.get("/provider/machines", headers=H)
machines = [x for x in r.json() if x["machine_id"] == str(mid)]
step("machine in GET /provider/machines", len(machines) == 1)
m = machines[0]
step("verification_status == detected (never verified)", m["verification_status"] == "detected", m["verification_status"])
step("not online before any heartbeat", m["status"] != "online", f"status={m['status']}")

# 9 REAL AGENT signed heartbeat
resp = heartbeat.send_contract_heartbeat(BASE, str(mid), msec, report=cap, availability="online")
code = getattr(resp, "status_code", 200)
step("agent signed heartbeat accepted", code == 200, f"HTTP {code}")

# 10 machine now ONLINE
r = c.get("/provider/machines", headers=H)
m = [x for x in r.json() if x["machine_id"] == str(mid)][0]
step("machine now ONLINE", m["status"] == "online", f"status={m['status']} last_hb={m['last_heartbeat_at']}")

# 11 tampered heartbeat (wrong secret) rejected
try:
    heartbeat.send_contract_heartbeat(BASE, str(mid), "msk_wrong_wrong_wrong", report=cap)
    step("tampered heartbeat rejected", False, "ACCEPTED!")
except Exception as e:
    step("tampered heartbeat rejected (401)", "Auth" in type(e).__name__, type(e).__name__)

# 12 account isolation: a different user cannot see this machine
email2 = f"other_{os.getpid()}@example.com"
r2 = c.post("/auth/register", json={"email": email2, "password": pw})
vtok2 = r2.json().get("email_verification_token") if r2.status_code in (200, 201) else None
if vtok2:
    c.post("/auth/verify-email", json={"token": vtok2})
a2 = c.post("/auth/login", json={"email": email2, "password": pw}).json()["access_token"]
r = c.get("/provider/machines", headers={"Authorization": f"Bearer {a2}"})
seen = r.status_code == 200 and any(x["machine_id"] == str(mid) for x in r.json())
step("account isolation (other user cannot see machine)", not seen, f"HTTP {r.status_code}")

print()
if fails:
    print(f"*** {fails} CHECK(S) FAILED ***")
    sys.exit(1)
print("ALL PHASE-2 END-TO-END CHECKS PASSED")
