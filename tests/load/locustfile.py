"""Locust scenarios for the ComputeCoin API (B4).

Run against the LOCAL stack only (docker-compose.yml, Postgres + Redis). The file
refuses a non-local host unless LOAD_ALLOW_REMOTE=1, which must only be set with the
owner's approval. Never point this at production.

Scenarios (one User class each, weighted):
  BrowseUser     public marketplace listing and detail          (no credentials)
  JobSubmitUser  login once, submit jobs, poll them             (LOAD_USER_*)
  AgentUser      signed heartbeat + claim loop                  (LOAD_MACHINE_*)
  InferenceUser  OpenAI-compatible chat completions             (LOAD_USER_*, LOAD_MODEL)

Environment:
  LOAD_USER_EMAIL, LOAD_USER_PASSWORD   a verified account with enough credit balance
  LOAD_MACHINE_ID, LOAD_MACHINE_SECRET  an enrolled machine ("msk_..." secret)
  LOAD_MODEL                            a registered inference model name
  LOAD_JOB_MODEL / LOAD_JOB_WORKLOAD    job model and workload_type for job submit
  LOAD_ALLOW_REMOTE=1                   permit a non-local --host (needs approval)

A User class whose credentials are missing stops itself on start, so a partial setup
still runs the scenarios it can.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import time
from urllib.parse import urlparse

from locust import HttpUser, between, events, task

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "api", "host.docker.internal"}


@events.init.add_listener
def _refuse_remote(environment, **_kwargs):
    host = environment.host or ""
    name = urlparse(host).hostname or ""
    if name not in _LOCAL_HOSTS and os.environ.get("LOAD_ALLOW_REMOTE") != "1":
        raise SystemExit(
            f"Refusing to load-test non-local host {host!r}. "
            "Use the local docker-compose stack, or set LOAD_ALLOW_REMOTE=1 with approval."
        )


def _env(name: str) -> str:
    return os.environ.get(name, "")


def _sign(machine_id: str, signing_key: str, body: bytes) -> dict[str, str]:
    """Same scheme as app/core/machine_auth.py: HMAC-SHA256 over
    '<machine_id>\\n<timestamp>\\n<sha256_hex(body)>' keyed by the part after 'msk_'."""
    ts = str(int(time.time()))
    canonical = f"{machine_id}\n{ts}\n{hashlib.sha256(body).hexdigest()}"
    sig = hmac.new(signing_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-ComputeCoin-Machine": machine_id,
        "X-ComputeCoin-Timestamp": ts,
        "X-ComputeCoin-Signature": sig,
    }


class BrowseUser(HttpUser):
    weight = 5
    wait_time = between(1, 3)

    def on_start(self):
        self.machine_ids: list[str] = []

    @task(5)
    def list_machines(self):
        params = {"page": random.randint(1, 3), "page_size": 20}
        if random.random() < 0.4:
            params["min_vram_mb"] = random.choice([8192, 16384, 24576])
        with self.client.get(
            "/v1/marketplace/machines", params=params, name="GET /v1/marketplace/machines",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"status {resp.status_code}")
                return
            items = resp.json().get("items", [])
            self.machine_ids = [str(m["id"]) for m in items if "id" in m] or self.machine_ids

    @task(2)
    def machine_detail(self):
        if self.machine_ids:
            self.client.get(
                f"/v1/marketplace/machines/{random.choice(self.machine_ids)}",
                name="GET /v1/marketplace/machines/{id}",
            )


class _AuthedUser(HttpUser):
    abstract = True

    def _login(self) -> bool:
        email, password = _env("LOAD_USER_EMAIL"), _env("LOAD_USER_PASSWORD")
        if not email or not password:
            return False
        with self.client.post(
            "/auth/login", json={"email": email, "password": password},
            name="POST /auth/login", catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"login status {resp.status_code}")
                return False
            self.client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        return True


class JobSubmitUser(_AuthedUser):
    weight = 2
    wait_time = between(2, 5)

    def on_start(self):
        self.job_ids: list[str] = []
        if not self._login():
            self.stop()

    @task(3)
    def submit_job(self):
        body = {
            "workload_type": _env("LOAD_JOB_WORKLOAD") or "text-generation",
            "model": _env("LOAD_JOB_MODEL") or "load-test-model",
            "input": {"prompt": "load test"},
            "estimated_minutes": 1,
        }
        with self.client.post(
            "/v1/jobs", json=body, name="POST /v1/jobs", catch_response=True
        ) as resp:
            if resp.status_code == 201:
                self.job_ids.append(str(resp.json()["id"]))
            else:
                resp.failure(f"status {resp.status_code}")

    @task(2)
    def poll_job(self):
        if self.job_ids:
            self.client.get(f"/v1/jobs/{random.choice(self.job_ids)}", name="GET /v1/jobs/{id}")

    @task(1)
    def list_jobs(self):
        self.client.get("/v1/jobs", name="GET /v1/jobs")


class AgentUser(HttpUser):
    """One simulated agent: signed heartbeat then claim, at the agent's cadence.

    All agent users share one enrolled machine; that exercises the signature path and
    claim locking, not machine fan-out. For fan-out, enrol N machines and extend this
    to read a LOAD_MACHINES_FILE (not implemented; see the baseline doc).
    """

    weight = 2
    wait_time = between(5, 10)

    def on_start(self):
        self.machine_id = _env("LOAD_MACHINE_ID")
        secret = _env("LOAD_MACHINE_SECRET")
        if not self.machine_id or not secret:
            self.stop()
            return
        self.signing_key = secret[4:] if secret.startswith("msk_") else secret

    def _post(self, path: str, payload: dict, name: str, ok=(200,)):
        body = json.dumps(payload, separators=(",", ":")).encode()
        with self.client.post(
            path, data=body, headers=_sign(self.machine_id, self.signing_key, body),
            name=name, catch_response=True,
        ) as resp:
            if resp.status_code in ok:
                resp.success()
            else:
                resp.failure(f"status {resp.status_code}")

    @task(3)
    def heartbeat(self):
        self._post("/v1/agents/heartbeat", {"availability": "online"},
                   "POST /v1/agents/heartbeat")

    @task(2)
    def claim(self):
        # 204/404 style "nothing to claim" answers are normal; only 5xx and 401 are failures.
        self._post("/v1/agents/jobs/claim", {"machine_id": self.machine_id},
                   "POST /v1/agents/jobs/claim", ok=(200, 204, 404))


class InferenceUser(_AuthedUser):
    weight = 1
    wait_time = between(2, 6)

    def on_start(self):
        if not _env("LOAD_MODEL") or not self._login():
            self.stop()

    @task
    def chat(self):
        body = {
            "model": _env("LOAD_MODEL"),
            "messages": [{"role": "user", "content": "Say hello."}],
            "max_tokens": 16,
        }
        # 429 is the per-user rate limit working as designed; it is counted as a failure
        # here on purpose so a baseline run notices it, and the report must say so.
        self.client.post("/v1/chat/completions", json=body,
                         name="POST /v1/chat/completions")
