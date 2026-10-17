"""Job runner: the claim -> execute -> result loop for the provider agent (Phase 4).

This module owns the agent side of the Phase 4 job protocol
(docs/api/phase4-execution-protocol.md). It:

* claims the next job reserved for THIS machine
  (``POST /v1/agents/jobs/claim``);
* runs it through the selected :class:`~computecoin_agent.executor.Executor`
  (simulated in-process, or a hardened container -- never arbitrary shell);
* streams log chunks (``POST /v1/agents/jobs/{id}/logs``);
* extends the lease with a heartbeat (``POST /v1/agents/jobs/{id}/heartbeat``);
* uploads produced artifacts via the short-lived signed upload grant;
* submits the idempotent receipt (``POST /v1/agents/jobs/{id}/result``).

Every agent endpoint is authenticated with the SAME machine HMAC scheme as the
Phase 2 heartbeat: headers ``X-ComputeCoin-Machine / -Timestamp / -Signature``
over the canonical string ``machine_id\\n ts \\n sha256(body)``, keyed by the
signing key derived from the ``msk_`` machine secret
(:func:`computecoin_agent.heartbeat.sign_contract`).

Honesty guarantees enforced here:

* The body that is signed is the EXACT body transmitted (serialized once).
* On executor failure the receipt is submitted with ``status="failed"`` and a
  SANITIZED error -- a failed job is NEVER reported as success, and no metrics
  are fabricated (they come straight from the executor).

The httpx transport is injectable, so the whole loop runs offline in tests.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from .executor import (
    Artifact,
    ExecutionResult,
    Executor,
    HardwareUnavailableError,
    JobSpec,
    select_executor,
)
from .heartbeat import (
    MACHINE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    canonical_bytes,
    sign_contract,
)

# Header carrying the short-lived, job-scoped artifact upload grant token.
UPLOAD_TOKEN_HEADER = "X-ComputeCoin-Upload-Token"

# Cap the sanitized error message length that leaves the host.
_MAX_ERROR_MESSAGE = 500


class JobRunnerError(RuntimeError):
    """A job-protocol transport/HTTP error (non-2xx where 2xx was required)."""


# --------------------------------------------------------------------------- #
# httpx client helper (mirrors heartbeat._client; transport is injectable)
# --------------------------------------------------------------------------- #
def _client(transport: Any, timeout: float):
    import httpx

    if transport is not None:
        return httpx.Client(transport=transport, timeout=timeout)
    return httpx.Client(timeout=timeout)


def _signed_headers(
    machine_id: str, machine_secret: str, raw_body: bytes, ts: int
) -> dict[str, str]:
    """Build the machine-HMAC auth headers for a job endpoint request.

    Identical scheme to the Phase 2 heartbeat: the signature is an HMAC-SHA256
    over ``machine_id\\n ts \\n sha256(raw_body)`` keyed by the signing key.
    """
    signature = sign_contract(machine_id, ts, raw_body, machine_secret)
    return {
        "Content-Type": "application/json",
        MACHINE_HEADER: machine_id,
        TIMESTAMP_HEADER: str(ts),
        SIGNATURE_HEADER: signature,
    }


def _signed_post(
    url: str,
    machine_id: str,
    machine_secret: str,
    body: dict[str, Any],
    *,
    transport: Any = None,
    timeout: float = 30.0,
    timestamp: Optional[int] = None,
) -> Any:
    """Serialize ``body`` ONCE, sign those exact bytes, and POST them."""
    raw = canonical_bytes(body)  # serialize once -> sign & send the same bytes
    ts = int(timestamp if timestamp is not None else time.time())
    headers = _signed_headers(machine_id, machine_secret, raw, ts)
    with _client(transport, timeout) as client:
        return client.post(url, content=raw, headers=headers)


# --------------------------------------------------------------------------- #
# Individual protocol calls
# --------------------------------------------------------------------------- #
def claim_job(
    base_url: str,
    machine_id: str,
    machine_secret: str,
    *,
    transport: Any = None,
    timestamp: Optional[int] = None,
    timeout: float = 30.0,
) -> Optional[dict[str, Any]]:
    """Claim the next job reserved for this machine.

    Returns the job-spec dict (RESERVED -> RUNNING, lease started) on 200, or
    ``None`` when the backend returns 204 (no job available). A non-{200,204}
    status raises :class:`JobRunnerError`.
    """
    if not machine_id or not machine_secret:
        raise RuntimeError(
            "machine is not enrolled; run `computecoin-agent enroll` first"
        )
    url = base_url.rstrip("/") + "/v1/agents/jobs/claim"
    resp = _signed_post(
        url,
        machine_id,
        machine_secret,
        {"machine_id": machine_id},
        transport=transport,
        timeout=timeout,
        timestamp=timestamp,
    )
    if resp.status_code == 204:
        return None
    if resp.status_code != 200:
        raise JobRunnerError(f"claim failed: HTTP {resp.status_code}")
    return resp.json()


def post_logs(
    base_url: str,
    machine_id: str,
    machine_secret: str,
    job_id: str,
    lines: list[str],
    *,
    transport: Any = None,
    timestamp: Optional[int] = None,
    timeout: float = 30.0,
) -> Any:
    """Append a log chunk for a RUNNING job (best-effort; signed)."""
    url = base_url.rstrip("/") + f"/v1/agents/jobs/{job_id}/logs"
    return _signed_post(
        url,
        machine_id,
        machine_secret,
        {"lines": list(lines)},
        transport=transport,
        timeout=timeout,
        timestamp=timestamp,
    )


def post_heartbeat(
    base_url: str,
    machine_id: str,
    machine_secret: str,
    job_id: str,
    *,
    progress: Optional[float] = None,
    metrics: Optional[dict[str, Any]] = None,
    transport: Any = None,
    timestamp: Optional[int] = None,
    timeout: float = 30.0,
) -> Any:
    """Heartbeat a RUNNING job to extend its lease and report progress."""
    url = base_url.rstrip("/") + f"/v1/agents/jobs/{job_id}/heartbeat"
    body: dict[str, Any] = {}
    if progress is not None:
        body["progress"] = progress
    if metrics is not None:
        body["metrics"] = metrics
    return _signed_post(
        url,
        machine_id,
        machine_secret,
        body,
        transport=transport,
        timeout=timeout,
        timestamp=timestamp,
    )


def upload_artifact(
    grant: dict[str, Any],
    artifact: Artifact,
    *,
    base_url: str | None = None,
    transport: Any = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Upload one artifact's bytes via the short-lived signed grant.

    The grant (from the claim response ``artifact_upload``) carries a ``url`` and
    a job-scoped ``token``. The bytes are PUT with the token in the
    ``X-ComputeCoin-Upload-Token`` header. Returns an artifact descriptor
    ``{artifact_id, kind, sha256, size, content_type}``; ``sha256``/``size`` are
    computed locally over the exact bytes sent, ``artifact_id`` comes from the
    store's response.
    """
    url = grant["url"]
    # The backend may return a RELATIVE grant url (e.g. /v1/artifacts/upload/<token>);
    # resolve it against base_url so the PUT targets an absolute URL.
    if base_url and not url.lower().startswith(("http://", "https://")):
        url = base_url.rstrip("/") + "/" + url.lstrip("/")
    token = grant["token"]
    headers = {
        "Content-Type": artifact.content_type,
        UPLOAD_TOKEN_HEADER: token,
    }
    with _client(transport, timeout) as client:
        resp = client.put(url, content=artifact.data, headers=headers)
    if resp.status_code not in (200, 201):
        raise JobRunnerError(f"artifact upload failed: HTTP {resp.status_code}")
    try:
        payload = resp.json() if resp.content else {}
    except ValueError:
        payload = {}
    descriptor = artifact.descriptor(artifact_id=payload.get("artifact_id"))
    return descriptor


def post_result(
    base_url: str,
    machine_id: str,
    machine_secret: str,
    job_id: str,
    body: dict[str, Any],
    *,
    transport: Any = None,
    timestamp: Optional[int] = None,
    timeout: float = 30.0,
) -> Any:
    """Submit the idempotent job receipt (signed)."""
    url = base_url.rstrip("/") + f"/v1/agents/jobs/{job_id}/result"
    resp = _signed_post(
        url,
        machine_id,
        machine_secret,
        body,
        transport=transport,
        timeout=timeout,
        timestamp=timestamp,
    )
    if resp.status_code != 200:
        raise JobRunnerError(f"result submission failed: HTTP {resp.status_code}")
    return resp


# --------------------------------------------------------------------------- #
# Result-body assembly
# --------------------------------------------------------------------------- #
def _sanitize_message(message: str) -> str:
    """Trim/collapse an error message before it leaves the host.

    Keeps a single-line, bounded string so we never leak internal paths across
    multiple lines or ship an unbounded blob.
    """
    collapsed = " ".join(str(message).split())
    if len(collapsed) > _MAX_ERROR_MESSAGE:
        collapsed = collapsed[:_MAX_ERROR_MESSAGE] + "..."
    return collapsed


def build_result_body(
    result: ExecutionResult, artifact_descriptors: list[dict[str, Any]]
) -> dict[str, Any]:
    """Assemble the ``/result`` body from an :class:`ExecutionResult`.

    A failed execution is reported ``status="failed"`` with a sanitized error and
    NO ``output`` -- a failure is never dressed up as success. Metrics come
    straight from the executor (no fabrication).
    """
    body: dict[str, Any] = {
        "status": result.status,
        "execution_mode": result.execution_mode,
        "artifacts": artifact_descriptors,
        "metrics": result.metrics,
        "evidence": {"output_schema_valid": bool(result.output_schema_valid)},
    }
    if result.status == "success":
        body["output"] = result.output
    else:
        err = result.error or {"code": "executor_error", "message": "execution failed"}
        body["error"] = {
            "code": _sanitize_message(err.get("code", "executor_error")),
            "message": _sanitize_message(err.get("message", "execution failed")),
        }
    return body


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_one(
    base_url: str,
    machine_id: str,
    machine_secret: str,
    *,
    transport: Any = None,
    capabilities: Optional[dict[str, Any]] = None,
    executor_selector: Callable[..., Executor] = select_executor,
    timestamp: Optional[int] = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Claim at most ONE job, execute it, and submit the receipt.

    Returns a small summary dict::

        {"claimed": False}                               # 204, nothing to do
        {"claimed": True, "job_id": ..., "status": ...}  # a job was processed

    On executor failure (including a hardware job with no usable Docker, which
    raises :class:`HardwareUnavailableError` rather than silently simulating) the
    receipt is submitted with ``status="failed"`` and a sanitized error.
    """
    claim = claim_job(
        base_url,
        machine_id,
        machine_secret,
        transport=transport,
        timestamp=timestamp,
        timeout=timeout,
    )
    if claim is None:
        return {"claimed": False}

    spec = JobSpec.from_claim(claim)
    grant = claim.get("artifact_upload") or {}

    executor = executor_selector(spec, capabilities)
    try:
        result = executor.run(spec)
    except HardwareUnavailableError as exc:
        result = ExecutionResult(
            status="failed",
            execution_mode=spec.execution_mode,
            metrics={
                "duration_seconds": 0.0,
                "max_memory_mb": None,
                "gpu_util_pct": None,
            },
            logs=[f"[agent] {exc}"],
            output_schema_valid=False,
            error={
                "code": "hardware_execution_unavailable",
                "message": str(exc),
            },
        )
    except Exception as exc:  # defensive: never crash the loop, report failure
        result = ExecutionResult(
            status="failed",
            execution_mode=spec.execution_mode,
            metrics={
                "duration_seconds": 0.0,
                "max_memory_mb": None,
                "gpu_util_pct": None,
            },
            logs=[f"[agent] unexpected executor error: {type(exc).__name__}"],
            output_schema_valid=False,
            error={"code": "executor_error", "message": str(exc)},
        )

    # Stream logs (best-effort; a logging failure must not sink the receipt).
    if result.logs:
        try:
            post_logs(
                base_url,
                machine_id,
                machine_secret,
                spec.job_id,
                result.logs,
                transport=transport,
                timestamp=timestamp,
                timeout=timeout,
            )
        except Exception:
            pass

    # Extend the lease before the (potentially slow) upload + result post.
    try:
        post_heartbeat(
            base_url,
            machine_id,
            machine_secret,
            spec.job_id,
            progress=1.0,
            metrics=result.metrics,
            transport=transport,
            timestamp=timestamp,
            timeout=timeout,
        )
    except Exception:
        pass

    # Upload artifacts via the signed grant (only when we actually have one).
    descriptors: list[dict[str, Any]] = []
    if grant.get("url") and grant.get("token"):
        for artifact in result.artifacts:
            descriptors.append(
                upload_artifact(grant, artifact, base_url=base_url, transport=transport, timeout=timeout)
            )

    body = build_result_body(result, descriptors)
    post_result(
        base_url,
        machine_id,
        machine_secret,
        spec.job_id,
        body,
        transport=transport,
        timestamp=timestamp,
        timeout=timeout,
    )
    return {"claimed": True, "job_id": spec.job_id, "status": result.status}


def poll_loop(
    base_url: str,
    machine_id: str,
    machine_secret: str,
    *,
    iterations: int = 0,
    interval: float = 5.0,
    transport: Any = None,
    capabilities: Optional[dict[str, Any]] = None,
    sleeper: Callable[[float], None] = time.sleep,
    emit: Optional[Callable[[int, dict[str, Any]], None]] = None,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """Run the claim->execute->result loop a bounded number of times.

    ``iterations <= 0`` loops until interrupted; a positive value runs exactly
    that many claim attempts (what tests use, with an injected ``transport`` and
    a no-op ``sleeper``). Transport/HTTP errors on a single iteration are caught
    so one bad claim never kills the loop. Returns the per-iteration summaries.
    """
    results: list[dict[str, Any]] = []
    i = 0
    while iterations <= 0 or i < iterations:
        try:
            summary = run_one(
                base_url,
                machine_id,
                machine_secret,
                transport=transport,
                capabilities=capabilities,
                timeout=timeout,
            )
        except Exception as exc:  # transient: record and keep polling
            summary = {"claimed": False, "error": str(exc)}
        results.append(summary)
        if emit is not None:
            emit(i, summary)
        i += 1
        if iterations <= 0 or i < iterations:
            # Back off only when there was nothing to do; a processed job loops
            # straight back to claim the next one.
            if not summary.get("claimed"):
                sleeper(interval)
    return results
