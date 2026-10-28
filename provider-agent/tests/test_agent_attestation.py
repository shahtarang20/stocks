"""Agent-side attestation tests (WP2, Checkpoints 2.1, 2.2, 2.3, 2.4)."""

import datetime as dt
import json
import pathlib
import httpx
import pytest

from computecoin_agent.attestation import (
    AttestationClientError,
    AttestationSession,
    generate_ephemeral_keypair,
    generate_receipt_signing_keypair,
    perform_attestation,
)
from computecoin_agent.config import AgentConfig


def test_ephemeral_and_signing_keys_stay_in_memory(tmp_path: pathlib.Path, monkeypatch):
    """Checkpoint 2.2: Private keys exist only in process memory; never written to config dir."""
    config_dir = tmp_path / "computecoin"
    config_dir.mkdir()
    config_file = config_dir / "agent.toml"
    config_file.write_text("[agent]\nmachine_id = 'test-id'\n")

    eph_priv, eph_pub = generate_ephemeral_keypair()
    sign_priv, sign_pub = generate_receipt_signing_keypair()

    session = AttestationSession(
        machine_id="test-id",
        isolation_tier="sealed",
        ephemeral_private_key=eph_priv,
        ephemeral_public_key=eph_pub,
        receipt_signing_private_key=sign_priv,
        receipt_signing_public_key=sign_pub,
        verdict={"verdict_id": "v-1", "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat()},
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30),
    )

    # Assert no private key bytes leaked to disk
    all_content = "".join(f.read_text() for f in config_dir.glob("**/*") if f.is_file())
    assert eph_priv not in all_content
    assert sign_priv not in all_content

    # Wipe session
    session.wipe()
    assert session.ephemeral_private_key == ""
    assert session.receipt_signing_private_key == ""


def test_verdict_ttl_and_reattest_logic():
    """Checkpoint 2.3: Expired verdict detected; re-attests at 2/3 of verdict TTL."""
    now = dt.datetime.now(dt.timezone.utc)

    # 1. Fresh session (30m remaining)
    fresh_session = AttestationSession(
        machine_id="test-id",
        isolation_tier="sealed",
        ephemeral_private_key="a" * 64,
        ephemeral_public_key="b" * 64,
        receipt_signing_private_key="c" * 64,
        receipt_signing_public_key="d" * 64,
        verdict={"verdict_id": "v-fresh", "issued_at": now.isoformat()},
        expires_at=now + dt.timedelta(minutes=30),
    )
    assert fresh_session.is_current(margin_seconds=60, now=now) is True
    assert fresh_session.should_reattest(now=now) is False

    # 2. Session near 2/3 TTL (9m remaining <= 10m)
    aging_session = AttestationSession(
        machine_id="test-id",
        isolation_tier="sealed",
        ephemeral_private_key="a" * 64,
        ephemeral_public_key="b" * 64,
        receipt_signing_private_key="c" * 64,
        receipt_signing_public_key="d" * 64,
        verdict={"verdict_id": "v-aging", "issued_at": (now - dt.timedelta(minutes=21)).isoformat()},
        expires_at=now + dt.timedelta(minutes=9),
    )
    assert aging_session.is_current(margin_seconds=60, now=now) is True
    assert aging_session.should_reattest(now=now) is True

    # 3. Expired session
    expired_session = AttestationSession(
        machine_id="test-id",
        isolation_tier="sealed",
        ephemeral_private_key="a" * 64,
        ephemeral_public_key="b" * 64,
        receipt_signing_private_key="c" * 64,
        receipt_signing_public_key="d" * 64,
        verdict={"verdict_id": "v-expired", "issued_at": (now - dt.timedelta(minutes=30)).isoformat()},
        expires_at=now - dt.timedelta(seconds=5),
    )
    assert expired_session.is_current(margin_seconds=0, now=now) is False


def test_perform_attestation_mock_client():
    """Checkpoint 2.1: perform_attestation runs challenge-response and obtains AttestationSession."""
    def handler(request: httpx.Request):
        if request.url.path == "/v1/agents/attestation/challenge":
            assert "X-ComputeCoin-Machine" in request.headers
            assert "X-ComputeCoin-Signature" in request.headers
            return httpx.Response(200, json={"machine_id": "m-123", "nonce": "01" * 24})
        elif request.url.path == "/v1/agents/attestation/verify":
            body = json.loads(request.content)
            assert body["nonce"] == "01" * 24
            assert body["isolation_tier"] == "sealed"
            assert "ephemeral_public_key" in body
            assert "receipt_signing_public_key" in body
            verdict = {
                "verdict_id": "v-mock-1",
                "machine_id": "m-123",
                "tier_reached": "sealed",
                "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat(),
                "signature": "sig",
            }
            return httpx.Response(200, json={"verified": True, "verdict": verdict, "details": {"log_leaf_index": 42}})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    session = perform_attestation(
        "http://testserver",
        "m-123",
        "msk_super-signing-key",
        tier="sealed",
        transport=transport,
    )
    assert session.machine_id == "m-123"
    assert session.isolation_tier == "sealed"
    assert session.verdict["verdict_id"] == "v-mock-1"
    assert session.log_leaf_index == 42
    assert session.is_current() is True


# --------------------------------------------------------------------------- private run_one path

import json as _json
import secrets as _secrets

import httpx as _httpx

from computecoin_agent.attestation import AttestationManager
from computecoin_agent.isolation import measure_security_state, probe_iommu, probe_kernel_lockdown
from computecoin_agent.jobrunner import run_one
from computecoin_agent.privacy_crypto import (
    INPUT_AAD,
    encrypt_data,
    open_sealed,
    output_aad,
    release_aad,
    seal,
    split_secret,
)


def _session(minutes_left=25, age_minutes=5):
    eph_priv, eph_pub = generate_ephemeral_keypair()
    sign_priv, sign_pub = generate_receipt_signing_keypair()
    now = dt.datetime.now(dt.timezone.utc)
    return AttestationSession(
        machine_id="m-1", isolation_tier="sealed", ephemeral_private_key=eph_priv, ephemeral_public_key=eph_pub,
        receipt_signing_private_key=sign_priv, receipt_signing_public_key=sign_pub,
        verdict={"verdict_id": "v-1", "issued_at": (now - dt.timedelta(minutes=age_minutes)).isoformat()},
        expires_at=now + dt.timedelta(minutes=minutes_left),
    )


class _PrivateBackend:
    """A fake backend for the WP-B flow. The claim carries NO keys, only a job-bound nonce; the
    verdict names the job; /keys wraps the shares to the host key the agent actually attested with."""

    def __init__(self, jobs=("job-7",), *, keys_status=200, keys_detail=None, verdict_job=None):
        self.queue = list(jobs)
        self.keys_status, self.keys_detail, self.verdict_job = keys_status, keys_detail, verdict_job
        self.dk = {j: _secrets.token_bytes(32) for j in jobs}
        self.cust_priv, self.cust_pub = generate_ephemeral_keypair()
        self.nonces: dict = {}
        self.calls: list = []
        self.verify_bodies: list = []
        self.keys_bodies: list = []
        self.results: dict = {}
        self.claim_bodies: list = []
        self.transport = _httpx.MockTransport(self.handle)

    def handle(self, request):
        path = request.url.path
        body = _json.loads(request.content or b"{}")
        self.calls.append(path)
        if path.endswith("/claim"):
            self.claim_bodies.append(body)
            if not body.get("accept_private") or not self.queue:
                return _httpx.Response(204)
            job = self.queue.pop(0)
            self.nonces[job] = _secrets.token_hex(24)
            return _httpx.Response(200, json={
                "job_id": job, "workload_type": "text-generation", "model": "echo-sim-v1", "input": {},
                "execution_mode": "simulated", "isolation_tier": "sealed", "limits": {},
                "artifact_upload": {"url": "/v1/artifacts/upload/t", "token": "t", "expires_at": "2099-01-01T00:00:00Z"},
                "lease_expires_at": "2099-01-01T00:00:00Z",
                "encrypted_input": encrypt_data(self.dk[job], _json.dumps({"prompt": "SECRET-MARKER"}).encode(), INPUT_AAD),
                "wrapped_shares": None, "verdict_id": None, "result_public_key": self.cust_pub,
                "key_attestation_required": True, "key_attestation_nonce": self.nonces[job],
            })
        if path.endswith("/attestation/challenge"):
            return _httpx.Response(200, json={"machine_id": "m-1", "nonce": "machine-level-nonce"})
        if path.endswith("/attestation/verify"):
            self.verify_bodies.append(body)
            job = next((j for j, n in self.nonces.items() if n == body["nonce"]), None)
            now = dt.datetime.now(dt.timezone.utc)
            verdict = {
                "verdict_id": f"v-{len(self.verify_bodies)}", "job_id": self.verdict_job or job, "machine_id": "m-1",
                "ephemeral_public_key": body["ephemeral_public_key"],
                "receipt_signing_public_key": body["receipt_signing_public_key"],
                "issued_at": now.isoformat(), "expires_at": (now + dt.timedelta(minutes=10)).isoformat(),
            }
            self.verdicts = getattr(self, "verdicts", {})
            self.verdicts[verdict["verdict_id"]] = verdict
            return _httpx.Response(200, json={"verified": True, "verdict": verdict, "details": {"log_leaf_index": 3}})
        if path.endswith("/keys"):
            job = path.split("/")[-2]
            self.keys_bodies.append((job, body))
            if self.keys_status != 200:
                return _httpx.Response(self.keys_status, json={"detail": self.keys_detail})
            verdict = self.verdicts[body["verdict_id"]]
            shares = split_secret(self.dk[job], 2, 2)
            return _httpx.Response(200, json={"job_id": job, "verdict_id": verdict["verdict_id"], "wrapped_shares": [
                {"guardian": g, "share_index": x, "wrapped_share": seal(verdict["ephemeral_public_key"], sh, release_aad(job, g))}
                for g, (x, sh) in zip(("computecoin", "partner"), shares)
            ]})
        if path.endswith("/result"):
            self.results[path.split("/")[-2]] = body
            return _httpx.Response(200, json={"status": "completed"})
        return _httpx.Response(200, json={})


def _recording_attest(sessions):
    def attest(*a, **kw):
        session = perform_attestation(*a, **kw)
        sessions.append(session)
        return session
    return attest


def test_run_one_attests_for_the_job_then_fetches_its_keys():
    backend = _PrivateBackend()
    machine_session = _session()
    sessions: list = []
    summary = run_one("http://api", "m-1", "msk_secret", transport=backend.transport,
                      attestation_session=machine_session, attest_fn=_recording_attest(sessions))
    assert summary == {"claimed": True, "job_id": "job-7", "status": "success"}
    assert backend.claim_bodies[0]["accept_private"] is True
    # The agent attested with the claim's job-bound nonce (no machine-level challenge) and fresh keys ...
    assert "/v1/agents/attestation/challenge" not in backend.calls
    assert backend.verify_bodies[0]["nonce"] == backend.nonces["job-7"]
    assert backend.verify_bodies[0]["ephemeral_public_key"] != machine_session.ephemeral_public_key
    # ... then fetched the keys with THAT verdict, before the result.
    assert backend.keys_bodies == [("job-7", {"verdict_id": "v-1"})]
    assert backend.calls.index("/v1/agents/jobs/job-7/keys") < backend.calls.index("/v1/agents/jobs/job-7/result")
    body = backend.results["job-7"]
    assert body["status"] == "success" and "output" not in body
    assert "SECRET-MARKER" not in _json.dumps(body)  # nothing plaintext goes back to the platform
    out = _json.loads(open_sealed(backend.cust_priv, body["encrypted_output"], output_aad("job-7")))
    assert "SECRET-MARKER" in _json.dumps(out)
    assert len(body["output_commitment"]) == 64 and body["wipe_receipt"]["job_id"] == "job-7"
    assert body["metrics"]["duration_seconds"] >= 0


def test_two_private_jobs_use_different_host_keys():
    """B7 (R5): each job gets its own attested key set; neither is the machine-level session's."""
    backend = _PrivateBackend(jobs=("job-a", "job-b"))
    machine_session = _session()
    sessions: list = []
    for _ in range(2):
        assert run_one("http://api", "m-1", "msk_secret", transport=backend.transport, attestation_session=machine_session,
                       attest_fn=_recording_attest(sessions))["status"] == "success"
    a, b = backend.verify_bodies
    assert a["nonce"] == backend.nonces["job-a"] and b["nonce"] == backend.nonces["job-b"]
    assert a["ephemeral_public_key"] != b["ephemeral_public_key"]
    assert a["receipt_signing_public_key"] != b["receipt_signing_public_key"]
    assert machine_session.ephemeral_public_key not in (a["ephemeral_public_key"], b["ephemeral_public_key"])
    assert [k[1]["verdict_id"] for k in backend.keys_bodies] == ["v-1", "v-2"]
    assert backend.results["job-a"]["wipe_receipt"]["job_id"] == "job-a"
    assert backend.results["job-b"]["wipe_receipt"]["job_id"] == "job-b"


@pytest.mark.parametrize("outcome", ["success", "keys_refused"])
def test_job_keys_are_dropped_after_the_job_and_never_written(tmp_path, monkeypatch, outcome):
    """B8: the job's private keys are wiped when the job ends (success or not) and never touch disk."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    backend = _PrivateBackend() if outcome == "success" else _PrivateBackend(keys_status=409, keys_detail="verdict_not_for_this_job")
    sessions: list = []
    run_one("http://api", "m-1", "msk_secret", transport=backend.transport, attestation_session=_session(),
            attest_fn=_recording_attest(sessions))
    assert len(sessions) == 1
    session = sessions[0]
    assert session.ephemeral_private_key == "" and session.receipt_signing_private_key == ""
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written == []  # nothing at all was written, so no key can be on disk


def test_a_verdict_for_another_job_is_never_used_to_fetch_keys():
    backend = _PrivateBackend(verdict_job="job-other")
    summary = run_one("http://api", "m-1", "msk_secret", transport=backend.transport, attestation_session=_session())
    assert summary["status"] == "failed"
    assert backend.keys_bodies == []
    assert backend.results["job-7"]["error"]["code"] == "job_attestation_failed"


def test_a_refused_key_request_reports_a_failed_job():
    backend = _PrivateBackend(keys_status=409, keys_detail="verdict_not_for_this_job")
    summary = run_one("http://api", "m-1", "msk_secret", transport=backend.transport, attestation_session=_session())
    assert summary["status"] == "failed"
    assert backend.results["job-7"]["status"] == "failed"
    assert backend.results["job-7"]["error"]["code"] == "verdict_not_for_this_job"


@pytest.mark.parametrize("status,detail", [(409, "key_release_failed"), (404, "job_not_found")])
def test_when_the_backend_already_ended_the_job_no_result_is_posted(status, detail):
    backend = _PrivateBackend(keys_status=status, keys_detail=detail)
    summary = run_one("http://api", "m-1", "msk_secret", transport=backend.transport, attestation_session=_session())
    assert summary == {"claimed": True, "job_id": "job-7", "status": "failed", "error": detail}
    assert backend.results == {}


def test_failed_job_attestation_reports_a_failed_job_without_asking_for_keys():
    backend = _PrivateBackend()

    def broken(*a, **kw):
        raise AttestationClientError("Attestation evidence rejected: job_not_attestable")

    summary = run_one("http://api", "m-1", "msk_secret", transport=backend.transport, attestation_session=_session(), attest_fn=broken)
    assert summary["status"] == "failed"
    assert backend.keys_bodies == []
    assert backend.results["job-7"]["error"]["code"] == "job_attestation_failed"


def test_without_a_session_the_agent_does_not_ask_for_private_jobs():
    backend = _PrivateBackend()
    assert run_one("http://api", "m-1", "msk_secret", transport=backend.transport, attestation_session=None) == {"claimed": False}
    assert backend.claim_bodies[0]["accept_private"] is False


def test_expired_session_does_not_ask_for_private_jobs():
    backend = _PrivateBackend()
    run_one("http://api", "m-1", "msk_secret", transport=backend.transport, attestation_session=_session(minutes_left=-1, age_minutes=31))
    assert backend.claim_bodies[0]["accept_private"] is False


def test_manager_attests_once_then_reattests_after_two_thirds():
    calls = []

    def fake_attest(*a, **kw):
        calls.append(1)
        return _session(minutes_left=30, age_minutes=0) if len(calls) == 1 else _session(minutes_left=30, age_minutes=0)

    mgr = AttestationManager("http://api", "m-1", "s", tier="sealed", attest_fn=fake_attest)
    first = mgr.current()
    assert mgr.current() is first and len(calls) == 1
    first.verdict["issued_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=21)).isoformat()
    first.expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=9)
    second = mgr.current()
    assert second is not first and len(calls) == 2
    assert first.ephemeral_private_key == ""  # old key dropped


def test_manager_failure_keeps_open_work_going():
    def broken(*a, **kw):
        raise RuntimeError("backend refused")

    errors = []
    mgr = AttestationManager("http://api", "m-1", "s", tier="sealed", attest_fn=broken, on_error=errors.append)
    assert mgr.current() is None and len(errors) == 1


def test_security_state_is_measured_not_assumed(tmp_path):
    lockdown = tmp_path / "lockdown"
    lockdown.write_text("none [integrity] confidentiality\n")
    assert probe_kernel_lockdown(lockdown_file=str(lockdown)) is True
    lockdown.write_text("[none] integrity confidentiality\n")
    assert probe_kernel_lockdown(lockdown_file=str(lockdown)) is False
    empty = tmp_path / "iommu"
    empty.mkdir()
    assert probe_iommu(iommu_dir=str(empty)) is False
    (empty / "dmar0").mkdir()
    assert probe_iommu(iommu_dir=str(empty)) is True
    assert set(measure_security_state()) == {"bare_metal", "iommu_enabled", "kernel_lockdown"}
