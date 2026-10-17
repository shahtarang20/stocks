"""Phase 4 jobrunner tests: claim -> execute -> upload -> result, all offline.

A single ``httpx.MockTransport`` emulates the whole backend and replicates the
server-side machine-HMAC verification on every signed endpoint.
"""

import hashlib
import hmac
import json

import httpx
import pytest

from computecoin_agent.executor import DockerExecutor, HardwareUnavailableError, JobSpec
from computecoin_agent.heartbeat import (
    MACHINE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
)
from computecoin_agent.jobrunner import UPLOAD_TOKEN_HEADER, run_one

MACHINE_ID = "11111111-1111-1111-1111-111111111111"
MACHINE_SECRET = "msk_" + "c" * 48
UPLOAD_URL = "https://storage.test/upload/job-1"
UPLOAD_TOKEN = "grant-token-abc"
BASE = "https://api.test"


def _verify_signature(request: httpx.Request) -> bool:
    """Server-side recomputation of the Phase 2 machine-HMAC signature."""
    raw = request.content
    machine = request.headers[MACHINE_HEADER]
    ts = int(request.headers[TIMESTAMP_HEADER])
    sig = request.headers[SIGNATURE_HEADER]
    if machine != MACHINE_ID:
        return False
    signing_key = MACHINE_SECRET[len("msk_"):]
    body_hash = hashlib.sha256(raw).hexdigest()
    canonical = f"{machine}\n{ts}\n{body_hash}"
    expected = hmac.new(
        signing_key.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


def make_backend(*, claim_response, captured):
    """Build a MockTransport emulating all agent job endpoints + artifact store."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path

        # Artifact upload PUT goes to the grant URL (not the API base).
        if request.method == "PUT" and str(request.url).startswith(UPLOAD_URL):
            assert request.headers.get(UPLOAD_TOKEN_HEADER) == UPLOAD_TOKEN
            data = request.content
            captured["artifact_bytes"] = data
            captured["artifact_content_type"] = request.headers.get("content-type")
            return httpx.Response(
                201,
                json={
                    "artifact_id": "art-xyz",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                },
            )

        # All API endpoints are signed; verify every one.
        assert _verify_signature(request), f"bad signature on {path}"

        if path.endswith("/jobs/claim"):
            body = json.loads(request.content)
            assert body["machine_id"] == MACHINE_ID
            captured["claim_verified"] = True
            if claim_response is None:
                return httpx.Response(204)
            return httpx.Response(200, json=claim_response)

        if path.endswith("/logs"):
            captured["logs"] = json.loads(request.content)["lines"]
            return httpx.Response(200, json={"ok": True})

        if path.endswith("/heartbeat"):
            captured["heartbeat"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})

        if path.endswith("/result"):
            captured["result"] = json.loads(request.content)
            status = captured["result"]["status"]
            return httpx.Response(
                200,
                json={
                    "job_id": "job-1",
                    "status": "completed" if status == "success" else "failed",
                },
            )

        return httpx.Response(404, json={"error": f"unexpected {path}"})

    return httpx.MockTransport(handler)


def _claim_simulated():
    return {
        "job_id": "job-1",
        "workload_type": "text-generation",
        "model": "echo-sim-v1",
        "input": {"prompt": "Hello world", "max_tokens": 32},
        "execution_mode": "simulated",
        "limits": {"max_runtime_seconds": 300, "memory_mb": 2048, "cpus": "1.0"},
        "image_digest": None,
        "artifact_upload": {
            "url": UPLOAD_URL,
            "token": UPLOAD_TOKEN,
            "expires_at": "2030-01-01T00:00:00Z",
        },
        "lease_expires_at": "2030-01-01T00:01:00Z",
    }


def test_run_one_simulated_success_full_flow():
    captured = {}
    transport = make_backend(claim_response=_claim_simulated(), captured=captured)

    summary = run_one(BASE, MACHINE_ID, MACHINE_SECRET, transport=transport)

    assert summary == {"claimed": True, "job_id": "job-1", "status": "success"}
    # The claim was correctly signed and verified server-side.
    assert captured["claim_verified"] is True

    # Artifact bytes were uploaded through the grant URL with the token, and the
    # bytes are exactly the JSON output the simulated executor produced.
    assert "artifact_bytes" in captured
    uploaded = json.loads(captured["artifact_bytes"])
    assert uploaded["simulated"] is True
    assert uploaded["execution_mode"] == "simulated"

    # The result receipt is signed, success, with honest metrics + evidence.
    result = captured["result"]
    assert result["status"] == "success"
    assert result["execution_mode"] == "simulated"
    assert "duration_seconds" in result["metrics"]
    assert result["evidence"]["output_schema_valid"] is True
    # Artifact descriptor carries the store-assigned id + locally computed sha256.
    assert len(result["artifacts"]) == 1
    art = result["artifacts"][0]
    assert art["artifact_id"] == "art-xyz"
    assert art["content_type"] == "application/json"
    assert len(art["sha256"]) == 64
    assert art["size"] == len(captured["artifact_bytes"])
    # Output is stamped simulated and surfaced.
    assert result["output"]["simulated"] is True


def test_run_one_204_is_noop():
    captured = {}
    transport = make_backend(claim_response=None, captured=captured)
    summary = run_one(BASE, MACHINE_ID, MACHINE_SECRET, transport=transport)
    assert summary == {"claimed": False}
    # No result was ever posted.
    assert "result" not in captured


def test_run_one_failed_executor_reports_failed_never_success():
    """An executor that fails must yield status=failed with a sanitized error."""
    captured = {}
    transport = make_backend(claim_response=_claim_simulated(), captured=captured)

    class FailingExecutor:
        def run(self, job_spec):
            raise HardwareUnavailableError("hardware execution unavailable: no docker")

    summary = run_one(
        BASE,
        MACHINE_ID,
        MACHINE_SECRET,
        transport=transport,
        executor_selector=lambda spec, caps: FailingExecutor(),
    )
    assert summary["status"] == "failed"
    result = captured["result"]
    assert result["status"] == "failed"
    assert "output" not in result          # no fabricated output
    assert result["error"]["code"] == "hardware_execution_unavailable"
    assert "unavailable" in result["error"]["message"]


def test_run_one_hardware_job_without_docker_reports_failed():
    """End-to-end: a real DockerExecutor with no Docker -> failed, not simulated."""
    captured = {}
    claim = _claim_simulated()
    claim["execution_mode"] = "hardware"
    claim["image_digest"] = "ghcr.io/acme/m@sha256:" + "d" * 64
    transport = make_backend(claim_response=claim, captured=captured)

    def selector(spec, caps):
        return DockerExecutor(docker_available=lambda: False)

    summary = run_one(
        BASE, MACHINE_ID, MACHINE_SECRET, transport=transport, executor_selector=selector
    )
    assert summary["status"] == "failed"
    result = captured["result"]
    assert result["status"] == "failed"
    assert result["execution_mode"] == "hardware"
    # Never silently simulated: no simulated stamp, no success.
    assert "output" not in result


def test_run_one_batch_inference_success():
    captured = {}
    claim = _claim_simulated()
    claim["workload_type"] = "batch-inference"
    claim["input"] = {"items": [{"x": 1}, {"x": 2}]}
    transport = make_backend(claim_response=claim, captured=captured)

    summary = run_one(BASE, MACHINE_ID, MACHINE_SECRET, transport=transport)
    assert summary["status"] == "success"
    out = captured["result"]["output"]
    assert out["count"] == len(out["items"]) == 2
    assert captured["result"]["evidence"]["output_schema_valid"] is True


def test_run_one_sends_logs_and_heartbeat():
    captured = {}
    transport = make_backend(claim_response=_claim_simulated(), captured=captured)
    run_one(BASE, MACHINE_ID, MACHINE_SECRET, transport=transport)
    assert captured.get("logs")               # log chunk was streamed
    assert captured.get("heartbeat") is not None  # lease extended
    assert captured["heartbeat"]["progress"] == 1.0
