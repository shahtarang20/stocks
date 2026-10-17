"""Phase 2 provider-protocol tests: real enroll + signed heartbeat against a
mocked backend (httpx.MockTransport) that emulates the contract.

These never hit the network and never touch the real ``~/.computecoin``: the
config dir is redirected to a temp path via ``COMPUTECOIN_CONFIG_DIR``.
"""

import hashlib
import hmac
import json
import stat

import httpx
import pytest

from computecoin_agent.config import AgentConfig
from computecoin_agent.enrollment import (
    EnrollmentError,
    RemoteEnrollmentResult,
    enroll_remote,
)
from computecoin_agent.hardware import build_capability_report
from computecoin_agent.heartbeat import (
    MACHINE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    contract_canonical_string,
    heartbeat_loop,
    send_contract_heartbeat,
    verify_contract_signature,
)

VALID_TOKEN = "enr_valid_0123456789"
MACHINE_ID = "11111111-1111-1111-1111-111111111111"
MACHINE_SECRET = "msk_" + "b" * 48


@pytest.fixture
def temp_home(monkeypatch, tmp_path):
    """Redirect the agent config dir to a temp path (no real ~/.computecoin)."""
    cfg_dir = tmp_path / ".computecoin"
    monkeypatch.setenv("COMPUTECOIN_CONFIG_DIR", str(cfg_dir))
    return cfg_dir


# --------------------------------------------------------------------------- #
# Backend emulator (contract-conformant)
# --------------------------------------------------------------------------- #
def enroll_backend(*, token=VALID_TOKEN, interval=30, online_window=90):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/agents/enroll"
        body = json.loads(request.content)
        # Contract honesty + shape checks.
        assert body["capability_report"]["verification_status"] == "detected"
        assert "agent_version" in body and "hostname" in body
        if body.get("enrollment_token") != token:
            return httpx.Response(401, json={"error": "token invalid/expired/used"})
        return httpx.Response(
            201,
            json={
                "machine_id": MACHINE_ID,
                "machine_secret": MACHINE_SECRET,
                "heartbeat_interval_seconds": interval,
                "online_window_seconds": online_window,
            },
        )

    return httpx.MockTransport(handler)


def heartbeat_backend(*, secret=MACHINE_SECRET, machine_id=MACHINE_ID, captured=None):
    """Emulates the server: recompute HMAC over the RAW body and verify."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/agents/heartbeat"
        raw = request.content  # the EXACT transmitted bytes
        hdr_machine = request.headers[MACHINE_HEADER]
        hdr_ts = int(request.headers[TIMESTAMP_HEADER])
        hdr_sig = request.headers[SIGNATURE_HEADER]
        if captured is not None:
            captured.append({"raw": raw, "headers": dict(request.headers)})

        if hdr_machine != machine_id:
            return httpx.Response(401, json={"error": "unknown machine"})

        # Server-side recomputation of the canonical string + HMAC.
        # The real backend keys the HMAC by the signing_key -- the machine
        # secret with its ``msk_`` prefix stripped (see the Phase 2 contract).
        signing_key = secret[len("msk_"):] if secret.startswith("msk_") else secret
        body_hash = hashlib.sha256(raw).hexdigest()
        canonical = f"{hdr_machine}\n{hdr_ts}\n{body_hash}"
        expected = hmac.new(
            signing_key.encode(), canonical.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, hdr_sig):
            return httpx.Response(401, json={"error": "bad signature"})

        return httpx.Response(200, json={"ok": True, "directives": {}})

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- #
# Enroll
# --------------------------------------------------------------------------- #
def test_enroll_success_persists_credential_0600(temp_home, no_nvml):
    report = build_capability_report()
    result = enroll_remote(
        "https://api.test", VALID_TOKEN, report, transport=enroll_backend(interval=45)
    )
    assert isinstance(result, RemoteEnrollmentResult)
    assert result.machine_id == MACHINE_ID
    assert result.machine_secret == MACHINE_SECRET
    assert result.heartbeat_interval_seconds == 45
    assert result.online_window_seconds == 90

    # Persisted with 0600 perms.
    assert result.config_path.exists()
    mode = stat.S_IMODE(result.config_path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    # Reloads from disk with the backend credential + interval.
    loaded = AgentConfig.load()
    assert loaded.machine_id == MACHINE_ID
    assert loaded.machine_secret == MACHINE_SECRET
    assert loaded.heartbeat_interval_seconds == 45
    assert loaded.has_machine_credential()
    mid, msec = AgentConfig.load_machine_credential()
    assert (mid, msec) == (MACHINE_ID, MACHINE_SECRET)


def test_enroll_accepts_contract_dict_and_sends_detected(temp_home, no_nvml):
    cap = build_capability_report().contract_capability_report()
    assert cap["verification_status"] == "detected"
    assert cap["gpus"] == []  # CPU-only
    result = enroll_remote(
        "https://api.test", VALID_TOKEN, cap, transport=enroll_backend()
    )
    assert result.machine_id == MACHINE_ID


def test_enroll_401_raises_and_writes_nothing(temp_home, no_nvml):
    report = build_capability_report()
    with pytest.raises(EnrollmentError):
        enroll_remote(
            "https://api.test",
            "enr_WRONG",
            report,
            transport=enroll_backend(),
        )
    # Nothing was written to disk.
    from computecoin_agent.config import default_config_path

    assert not default_config_path().exists()


# --------------------------------------------------------------------------- #
# Heartbeat signing (contract)
# --------------------------------------------------------------------------- #
def test_heartbeat_canonical_string_and_signature_verify(no_nvml):
    captured = []
    report = build_capability_report()
    resp = send_contract_heartbeat(
        "https://api.test",
        MACHINE_ID,
        MACHINE_SECRET,
        report=report,
        transport=heartbeat_backend(captured=captured),
        timestamp=1700000000,
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "directives": {}}

    # Replicate the server verify from the captured raw bytes + headers.
    rec = captured[0]
    raw = rec["raw"]
    ts = int(rec["headers"]["x-computecoin-timestamp"])
    sig = rec["headers"]["x-computecoin-signature"]
    machine = rec["headers"]["x-computecoin-machine"]
    assert machine == MACHINE_ID
    assert ts == 1700000000

    # The canonical string is EXACTLY the contract form.
    expected_canonical = (
        f"{MACHINE_ID}\n1700000000\n{hashlib.sha256(raw).hexdigest()}"
    )
    assert contract_canonical_string(MACHINE_ID, 1700000000, raw) == expected_canonical

    # Server-side recomputation verifies.
    assert verify_contract_signature(MACHINE_ID, ts, raw, sig, MACHINE_SECRET)

    # Tamper the body -> verification would fail.
    tampered = raw + b" "
    assert not verify_contract_signature(MACHINE_ID, ts, tampered, sig, MACHINE_SECRET)
    # Wrong secret -> fails.
    assert not verify_contract_signature(MACHINE_ID, ts, raw, sig, "msk_other")


def test_heartbeat_sends_exact_bytes_it_signed(no_nvml):
    """Regression against double-serialization: the signed bytes == sent bytes."""
    captured = []
    report = build_capability_report()
    send_contract_heartbeat(
        "https://api.test",
        MACHINE_ID,
        MACHINE_SECRET,
        report=report,
        transport=heartbeat_backend(captured=captured),
        timestamp=1700000001,
    )
    raw = captured[0]["raw"]
    sig = captured[0]["headers"]["x-computecoin-signature"]
    # The signature in the header must match an HMAC over the sha256 of the
    # EXACT bytes transmitted (not a re-serialization).
    body_hash = hashlib.sha256(raw).hexdigest()
    canonical = f"{MACHINE_ID}\n1700000001\n{body_hash}"
    # HMAC keyed by the signing_key (machine secret minus the ``msk_`` prefix).
    signing_key = MACHINE_SECRET[len("msk_"):]
    expected = hmac.new(
        signing_key.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()
    assert sig == expected
    # And the transmitted body is valid JSON with the contract shape.
    body = json.loads(raw)
    assert set(body) == {"availability", "capability_summary", "health"}
    assert set(body["capability_summary"]) == {"gpu_count", "vram_mb"}


def test_heartbeat_bad_signature_rejected_401(no_nvml):
    from computecoin_agent.heartbeat import HeartbeatAuthError

    report = build_capability_report()
    # Backend knows a DIFFERENT secret -> signature mismatch -> 401.
    with pytest.raises(HeartbeatAuthError):
        send_contract_heartbeat(
            "https://api.test",
            MACHINE_ID,
            MACHINE_SECRET,
            report=report,
            transport=heartbeat_backend(secret="msk_server_knows_other"),
        )


def test_heartbeat_requires_credential(no_nvml):
    with pytest.raises(RuntimeError):
        send_contract_heartbeat("https://api.test", "", "", report=build_capability_report())


# --------------------------------------------------------------------------- #
# Run loop
# --------------------------------------------------------------------------- #
def test_run_loop_sends_n_heartbeats_and_stops(no_nvml):
    captured = []
    sleeps = []
    report = build_capability_report()
    responses = heartbeat_loop(
        "https://api.test",
        MACHINE_ID,
        MACHINE_SECRET,
        report=report,
        iterations=4,
        interval=0.0,
        transport=heartbeat_backend(captured=captured),
        sleeper=sleeps.append,
    )
    assert len(responses) == 4
    assert all(r.status_code == 200 for r in responses)
    assert len(captured) == 4  # backend saw exactly 4 heartbeats
    assert len(sleeps) == 3  # sleeps between, not after the last
