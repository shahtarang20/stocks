"""Signed-heartbeat tests with a mocked, offline transport."""

import pytest

from computecoin_agent.config import AgentConfig
from computecoin_agent.hardware import build_capability_report
from computecoin_agent.heartbeat import (
    MACHINE_HEADER,
    SIGNATURE_HEADER,
    build_heartbeat_payload,
    canonical_bytes,
    send_heartbeat,
    sign_payload,
    verify_signature,
)


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {"ok": True}

    def json(self):
        return self._body


class RecordingTransport:
    """Captures the request so tests can assert on it; never hits the network."""

    def __init__(self, status_code=200):
        self.calls = []
        self.status_code = status_code

    def post(self, url, *, content, headers):
        self.calls.append({"url": url, "content": content, "headers": headers})
        return FakeResponse(self.status_code)


def _enrolled_config():
    return AgentConfig(
        api_url="https://api.test",
        machine_id="mch_test",
        signing_secret="a" * 64,
    )


def test_signature_round_trips(no_nvml):
    report = build_capability_report()
    payload = build_heartbeat_payload("mch_x", report, timestamp=1000)
    sig = sign_payload(payload, "secret123")
    assert verify_signature(payload, sig, "secret123")
    assert not verify_signature(payload, sig, "wrong-secret")


def test_tampered_payload_fails_verification(no_nvml):
    report = build_capability_report()
    payload = build_heartbeat_payload("mch_x", report, timestamp=1000)
    sig = sign_payload(payload, "secret123")
    payload["health"] = "tampered"
    assert not verify_signature(payload, sig, "secret123")


def test_send_heartbeat_signs_and_verifies(no_nvml):
    config = _enrolled_config()
    transport = RecordingTransport()
    report = build_capability_report()
    resp = send_heartbeat(config, report=report, transport=transport, timestamp=42)

    assert resp.status_code == 200
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://api.test/v1/agents/heartbeat"
    assert call["headers"][MACHINE_HEADER] == "mch_test"

    # Reconstruct payload from the transmitted bytes and verify the signature.
    import json

    payload = json.loads(call["content"])
    assert payload["machine_id"] == "mch_test"
    assert payload["timestamp"] == 42
    # canonical_bytes of the reconstructed payload must match what was sent.
    assert canonical_bytes(payload) == call["content"]
    assert verify_signature(payload, call["headers"][SIGNATURE_HEADER], "a" * 64)


def test_send_heartbeat_requires_enrollment(no_nvml):
    config = AgentConfig(api_url="https://api.test")  # not enrolled
    with pytest.raises(RuntimeError):
        send_heartbeat(config, transport=RecordingTransport())


def test_capability_summary_included_in_payload(no_nvml):
    config = _enrolled_config()
    transport = RecordingTransport()
    send_heartbeat(config, transport=transport, timestamp=1)
    import json

    payload = json.loads(transport.calls[0]["content"])
    assert "capability_summary" in payload
    assert payload["capability_summary"]["status"] == "DETECTED"
