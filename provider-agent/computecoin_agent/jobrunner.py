"""Job runner: the claim -> execute -> result loop for the provider agent (Phase 4).

This module owns the agent side of the Phase 4 job protocol
(docs/api/phase4-execution-protocol.md). It:

* claims the next job reserved for THIS machine
  (``POST /v1/agents/jobs/claim``);
* runs it through the selected :class:`~computecoin_agent.executor.Executor`
  (simulated in-process, or a hardened container -- never arbitrary shell);
* streams log chunks (``POST /v1/agents/jobs/{id}/logs``);
* extends the lease with a heartbeat (``POST /v1/agents/jobs/{id}/heartbeat``);
* for a private job, attests fresh in-RAM keys for THAT job and fetches its key shares once
  (``POST /v1/agents/jobs/{id}/keys``);
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

import dataclasses
import time
from typing import Any, Callable, Optional

from .attestation import AttestationSession, perform_attestation
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
from .privacy import execute_sealed_job

# Header carrying the short-lived, job-scoped artifact upload grant token.
UPLOAD_TOKEN_HEADER = "X-ComputeCoin-Upload-Token"

# Cap the sanitized error message length that leaves the host.
_MAX_ERROR_MESSAGE = 500


class JobRunnerError(RuntimeError):
    """A job-protocol transport/HTTP error (non-2xx where 2xx was required)."""


class KeyReleaseError(JobRunnerError):
    """The backend refused to release a private job's keys.

    ``job_closed`` is True when the backend already ended the job itself (it fails the job when
    the guardians refuse, and a 404 means the job is no longer ours), so no result is posted."""

    def __init__(self, message: str, *, code: str, job_closed: bool):
        super().__init__(message)
        self.code = code
        self.job_closed = job_closed


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
    accept_private: bool = False,
) -> Optional[dict[str, Any]]:
    """Claim the next job reserved for this machine.

    ``accept_private`` says this agent holds a current attestation session; without it the
    backend never hands out a private job (no keys are released to a host that cannot use them).

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
        {"machine_id": machine_id, "accept_private": bool(accept_private)},
        transport=transport,
        timeout=timeout,
        timestamp=timestamp,
    )
    if resp.status_code == 204:
        return None
    if resp.status_code != 200:
        raise JobRunnerError(f"claim failed: HTTP {resp.status_code}")
    return resp.json()


def fetch_job_keys(
    base_url: str,
    machine_id: str,
    machine_secret: str,
    job_id: str,
    verdict_id: str,
    *,
    transport: Any = None,
    timestamp: Optional[int] = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Fetch a private job's key shares, wrapped to the host key of the job-bound verdict.

    The backend releases them once per job; any refusal raises :class:`KeyReleaseError`.
    """
    url = base_url.rstrip("/") + f"/v1/agents/jobs/{job_id}/keys"
    resp = _signed_post(
        url,
        machine_id,
        machine_secret,
        {"verdict_id": verdict_id},
        transport=transport,
        timeout=timeout,
        timestamp=timestamp,
    )
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        code = detail if isinstance(detail, str) else "key_release_refused"
        raise KeyReleaseError(
            f"key release refused: HTTP {resp.status_code} {code}",
            code=code,
            job_closed=resp.status_code == 404 or code == "key_release_failed",
        )
    data = resp.json()
    shares = data.get("wrapped_shares")
    if data.get("job_id") != job_id or data.get("verdict_id") != verdict_id or not isinstance(shares, list) or not shares:
        raise KeyReleaseError("key release answered for another job or verdict", code="key_release_mismatch", job_closed=False)
    return shares

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
    if resp.status_code == 409:
        # job_not_running: the platform cancelled/ended this job (customer cancel, a failed sibling rank, a replica start
        # timeout). Final: never retried, nothing more is sent, and the API did not touch billing.
        return resp
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
        if result.encrypted_output is not None:
            body["encrypted_output"] = result.encrypted_output
            if result.output_commitment is not None:
                body["output_commitment"] = result.output_commitment
            if result.wipe_receipt is not None:
                body["wipe_receipt"] = result.wipe_receipt
        else:
            body["output"] = result.output
    else:
        err = result.error or {"code": "executor_error", "message": "execution failed"}
        body["error"] = {
            "code": _sanitize_message(err.get("code", "executor_error")),
            "message": _sanitize_message(err.get("message", "execution failed")),
        }
        if result.wipe_receipt is not None:
            body["wipe_receipt"] = result.wipe_receipt
    return body


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
MAX_PRIVATE_CHAT_MESSAGES = 512


def normalize_private_chat_input(data: Any) -> dict[str, Any]:
    """Decrypted private chat input contract: {"prompt": str, "messages": [{"role","content"}], "max_tokens"?, "temperature"?}.

    ``messages`` is preferred; a bare ``prompt`` becomes one user message; ``prompt`` is re-rendered as "role: content" when
    missing. Raises ValueError with a fixed text (never quoting the data: the decrypted input is private)."""
    if not isinstance(data, dict):
        raise ValueError("private chat input must be an object")
    raw = data.get("messages")
    msgs: list[dict[str, str]] = []
    if isinstance(raw, list) and raw:
        if len(raw) > MAX_PRIVATE_CHAT_MESSAGES:
            raise ValueError("too many chat messages")
        for m in raw:
            if not isinstance(m, dict) or not isinstance(m.get("content"), str):
                raise ValueError("each chat message needs string role and content")
            role = m.get("role", "user")
            if not isinstance(role, str) or not role:
                raise ValueError("each chat message needs string role and content")
            msgs.append({"role": role, "content": m["content"]})
    else:
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("private chat input needs messages or a non-empty prompt")
        msgs = [{"role": "user", "content": prompt}]
    out: dict[str, Any] = {"messages": msgs}
    prompt = data.get("prompt")
    out["prompt"] = prompt if isinstance(prompt, str) and prompt else "\n".join(f"{m['role']}: {m['content']}" for m in msgs)
    mt = data.get("max_tokens")
    if mt is not None:
        if isinstance(mt, bool) or not isinstance(mt, int) or mt < 1:
            raise ValueError("max_tokens must be a positive integer")
        out["max_tokens"] = mt
    temp = data.get("temperature")
    if temp is not None:
        if isinstance(temp, bool) or not isinstance(temp, (int, float)) or not 0 <= temp <= 2:
            raise ValueError("temperature must be a number between 0 and 2")
        out["temperature"] = temp
    return out


def _run_private(
    spec: JobSpec,
    session: AttestationSession,
    machine_id: str,
    capabilities: Any,
    executor_selector: Callable[..., Executor],
    stream_monitor: Any = None,
    kit_ctx: Any = None,
) -> ExecutionResult:
    """Unwrap the Data Key in RAM, run the workload on the decrypted input, seal the output.

    ``kit_ctx`` (a :class:`~computecoin_agent.private_kit.PrivateKitContext`): a private Kit job (U18). The Kit container
    runs on RAM-backed staging with no network and its outputs are sealed before they leave the host."""
    captured_metrics: dict[str, Any] = {}

    def workload_runner(decrypted_input: dict[str, Any]) -> dict[str, Any]:
        if kit_ctx is not None:
            from .private_kit import run_private_kit

            try:
                return run_private_kit(spec, decrypted_input, kit_ctx, executor_selector, capabilities)
            finally:
                captured_metrics.update(kit_ctx.metrics)
        # Opportunistic RAM-only output dir (holds input.json and, for sealed streaming, the stream pipe). With no RAM
        # dir the run keeps its old behaviour, except that a sealed stream is then switched off (never a plaintext file).
        from .private_kit import PrivateKitError, ram_workdir, wipe_dir

        ram: Optional[str] = None
        try:
            ram = ram_workdir(f"ccoin-priv-{spec.job_id[:8]}-")
        except PrivateKitError:
            ram = None
        try:
            return _run_workload(decrypted_input, ram)
        finally:
            if ram is not None:
                wipe_dir(ram)

    def _run_workload(decrypted_input: dict[str, Any], ram: Optional[str]) -> dict[str, Any]:
        if spec.workload_type == "text-generation":
            decrypted_input = normalize_private_chat_input(decrypted_input)
        sub_spec = JobSpec(
            job_id=spec.job_id,
            workload_type=spec.workload_type,
            model=spec.model,
            input=decrypted_input,
            execution_mode=spec.execution_mode,
            image_digest=spec.image_digest,
            limits=spec.limits,
            isolation_tier=spec.isolation_tier,
        )
        if ram is not None:
            sub_spec.output_dir = ram
        sub_executor = executor_selector(sub_spec, capabilities)
        monitor = stream_monitor if ram is not None else None  # no RAM dir: no stream, rather than a plaintext file
        if monitor is not None:  # U18: sealed streaming; the monitor seals every chunk before it leaves
            sub_spec.on_output_dir = monitor.set_output_dir
            sub_spec.extra_env = {**sub_spec.extra_env, **monitor.env}
            monitor.start()
        try:
            exec_res = sub_executor.run(sub_spec)
        finally:
            if monitor is not None:
                monitor.stop()
        if exec_res.status != "success":
            raise RuntimeError(
                exec_res.error.get("message", "workload failed")
                if exec_res.error
                else "workload failed"
            )
        if exec_res.metrics:
            captured_metrics.update(exec_res.metrics)
        return exec_res.output

    started = time.monotonic()
    try:
        sealed_res = execute_sealed_job(
            job_id=spec.job_id,
            machine_id=machine_id,
            ephemeral_private_key=session.ephemeral_private_key,
            receipt_signing_private_key=session.receipt_signing_private_key,
            result_public_key=spec.result_public_key,
            encrypted_input=spec.encrypted_input,
            wrapped_shares=spec.wrapped_shares,
            workload_runner_fn=workload_runner,
            scratch_dir=kit_ctx.ram_dir if kit_ctx is not None else None,
        )
        result = ExecutionResult(
            status="success",
            execution_mode=spec.execution_mode,
            # Billing reads duration_seconds: measure it here if the workload did not report it.
            metrics={"duration_seconds": round(time.monotonic() - started, 3), **captured_metrics},
            logs=[],
            output_schema_valid=True,
            encrypted_output=sealed_res.encrypted_output,
            output_commitment=sealed_res.output_commitment,
            wipe_receipt=sealed_res.wipe_receipt,
        )
    except Exception as exc:
        from .private_kit import PrivateKitError

        result = ExecutionResult(
            status="failed",
            execution_mode=spec.execution_mode,
            metrics={"duration_seconds": round(time.monotonic() - started, 3)},
            logs=[],
            output_schema_valid=False,
            # Generic on purpose: an exception text could quote the decrypted input. A PrivateKitError code is a fixed
            # identifier chosen by this agent (never derived from the data), so it may be reported.
            error={"code": exc.code if isinstance(exc, PrivateKitError) else "sealed_execution_error",
                   "message": f"sealed execution failed ({type(exc).__name__})"},
        )
    return result


def _failed_private(spec: JobSpec, code: str, message: str) -> ExecutionResult:
    return ExecutionResult(
        status="failed",
        execution_mode=spec.execution_mode,
        metrics={"duration_seconds": 0.0},
        logs=[],
        output_schema_valid=False,
        error={"code": code, "message": message},
    )


def _run_private_claim(
    base_url: str,
    machine_id: str,
    machine_secret: str,
    spec: JobSpec,
    claim: dict[str, Any],
    capabilities: Any,
    executor_selector: Callable[..., Executor],
    *,
    transport: Any,
    timestamp: Optional[int],
    timeout: float,
    attest_fn: Callable[..., AttestationSession],
    hardware: Optional[Callable[[str], tuple[str, dict[str, Any]]]] = None,
) -> ExecutionResult:
    """One job, one key set (R5): fresh in-RAM keys are attested for THIS job with the claim's
    job-bound challenge, the shares are fetched once with that verdict, and the keys are wiped
    when the job ends, so nothing this host holds afterwards can open the job's data.

    Raises :class:`KeyReleaseError` when the backend refuses the shares."""
    nonce = claim.get("key_attestation_nonce")
    missing = [
        name
        for name, value in (
            ("key_attestation_nonce", nonce),
            ("encrypted_input", spec.encrypted_input),
            ("result_public_key", spec.result_public_key),
        )
        if not value
    ]
    if missing:
        return _failed_private(spec, "private_claim_incomplete", f"private claim is missing {', '.join(missing)}")

    # U18: a private Kit job (fan-out part). Only the minimal private plan is accepted, and the RAM-only work dir must
    # exist BEFORE any key is released (no RAM dir = refuse; plaintext never falls back to disk).
    kit_ctx: Any = None
    if spec.v2 is not None:
        from .private_kit import PrivateKitContext, PrivateKitError, parse_private_plan, ram_workdir
        from .runtime_v2 import V2Api

        try:
            kit_plan = parse_private_plan(spec.v2)
            from .config import AgentConfig

            try:
                ram_base = AgentConfig.load().private_ram_dir
            except Exception:
                ram_base = None
            kit_ctx = PrivateKitContext(
                kit_plan, ram_workdir(f"ccoin-pk-{spec.job_id[:8]}-", ram_base),
                V2Api(base_url, machine_id, machine_secret, transport=transport, timeout=timeout), transport=transport,
            )
        except PrivateKitError as exc:
            return _failed_private(spec, exc.code, "the private Kit plan was refused on this host")

    session: Optional[AttestationSession] = None
    try:
        try:
            session = attest_fn(
                base_url, machine_id, machine_secret,
                tier=spec.isolation_tier, transport=transport, timeout=timeout, nonce=nonce,
                **({"hardware": hardware} if hardware is not None else {}),
            )
        except Exception as exc:
            return _failed_private(spec, "job_attestation_failed", f"attestation for this job failed ({type(exc).__name__})")
        verdict = session.verdict or {}
        if verdict.get("job_id") != spec.job_id or not verdict.get("verdict_id"):
            return _failed_private(spec, "job_attestation_failed", "the verdict is not bound to this job")
        shares = fetch_job_keys(
            base_url, machine_id, machine_secret, spec.job_id, verdict["verdict_id"],
            transport=transport, timestamp=timestamp, timeout=timeout,
        )
        job_spec = dataclasses.replace(spec, wrapped_shares=shares, verdict_id=verdict["verdict_id"])
        monitor = None
        if spec.stream_sealed and spec.workload_type == "text-generation":
            try:
                from .jobmonitor import JobMonitor

                monitor = JobMonitor(base_url, machine_id, machine_secret, spec.job_id, stream=True,
                                     transport=transport, seal_to=spec.result_public_key)
            except Exception:
                monitor = None  # streaming is best-effort; the sealed final result still arrives
        return _run_private(job_spec, session, machine_id, capabilities, executor_selector, stream_monitor=monitor, kit_ctx=kit_ctx)
    finally:
        if session is not None:
            session.wipe()
        if kit_ctx is not None:
            from .private_kit import wipe_dir

            wipe_dir(kit_ctx.ram_dir)


def _make_monitor(
    base_url: str, machine_id: str, machine_secret: str, spec: JobSpec, transport: Any
) -> Any:
    """U19/B2: a JobMonitor for a real (hardware, non-private) job; None otherwise or on any error."""
    if spec.execution_mode != "hardware":
        return None
    try:
        from .jobmonitor import JobMonitor

        import threading

        spec.cancel_event = threading.Event()  # U13: the monitor sets it when the API cancelled the job
        return JobMonitor(
            base_url, machine_id, machine_secret, spec.job_id,
            stream=spec.workload_type == "text-generation", transport=transport,
            cancel_event=spec.cancel_event,
            # U16: BYOC containers (named ccoin-job-<id> by the executor) get process-name / I/O sampling.
            byoc_container=f"ccoin-job-{spec.job_id}" if (spec.v2 or {}).get("byoc") is not None else None,
        )
    except Exception:
        return None


def _run_open(
    spec: JobSpec,
    capabilities: Any,
    executor_selector: Callable[..., Executor],
    monitor: Any = None,
) -> ExecutionResult:
    """Run a non-private job on its plaintext input; executor errors become a failed result.

    ``monitor`` (a :class:`~computecoin_agent.jobmonitor.JobMonitor`, hardware jobs only) reports
    progress / GPU util / VRAM and streams ordered token chunks while the executor runs.
    """
    executor = executor_selector(spec, capabilities)
    try:
        if monitor is not None:
            spec.on_output_dir = monitor.set_output_dir
            spec.extra_env = {**spec.extra_env, **monitor.env}
            monitor.start()
        try:
            result = executor.run(spec)
        finally:
            if monitor is not None:
                monitor.stop()
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
    return result


def _run_v2(
    base_url: str,
    machine_id: str,
    machine_secret: str,
    spec: JobSpec,
    capabilities: Any,
    executor_selector: Callable[..., Executor],
    *,
    transport: Any = None,
    timestamp: Optional[int] = None,
    timeout: float = 60.0,
) -> tuple[ExecutionResult, list[dict[str, Any]]]:
    """U3: a job whose claim carries a v2 plan (staged inputs, host cache, checkpoints, data-plane outputs)."""
    from .config import AgentConfig
    from .runtime_v2 import V2Api, context_from_config, run_v2

    try:
        config = AgentConfig.load()
    except Exception:
        config = AgentConfig()

    def beat() -> None:
        post_heartbeat(base_url, machine_id, machine_secret, spec.job_id, progress=None, metrics=None,
                       transport=transport, timestamp=timestamp, timeout=timeout)

    api = V2Api(base_url, machine_id, machine_secret, transport=transport, timeout=timeout)
    ctx = context_from_config(config, api, transport=transport, heartbeat=beat)
    mon = _make_monitor(base_url, machine_id, machine_secret, spec, transport)
    return run_v2(spec, ctx, lambda s: _run_open(s, capabilities, executor_selector, mon))


def _run_vm(
    base_url: str,
    machine_id: str,
    machine_secret: str,
    spec: JobSpec,
    *,
    transport: Any = None,
    timestamp: Optional[int] = None,
    timeout: float = 60.0,
) -> tuple[ExecutionResult, list[dict[str, Any]]]:
    """U14: a claim whose v2 block says ``mode: "vm"``. Runs only with ``windows_vm_enabled`` on this agent and a host that
    advertises vfio_gpu; otherwise it is reported failed (never run as an ordinary Kit job)."""
    from .config import AgentConfig
    from .runtime_v2 import V2Api, context_from_config, run_vm_v2

    try:
        config = AgentConfig.load()
    except Exception:
        config = AgentConfig()

    def beat() -> None:
        post_heartbeat(base_url, machine_id, machine_secret, spec.job_id, progress=None, metrics=None,
                       transport=transport, timestamp=timestamp, timeout=timeout)

    api = V2Api(base_url, machine_id, machine_secret, transport=transport, timeout=timeout)
    ctx = context_from_config(config, api, transport=transport, heartbeat=beat)
    return run_vm_v2(spec, ctx, windows_vm_enabled=bool(getattr(config, "windows_vm_enabled", False)))


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
    attestation_session: Optional[AttestationSession] = None,
    attest_fn: Callable[..., AttestationSession] = perform_attestation,
    hardware: Optional[Callable[[str], tuple[str, dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Claim at most ONE job, execute it, and submit the receipt.

    ``attestation_session`` is the machine-level session: it only tells the backend this agent
    may take private work. A private job never uses it for keys (R5, one job, one key set).

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
        accept_private=attestation_session is not None and not attestation_session.is_expired(),
    )
    if claim is None:
        return {"claimed": False}

    spec = JobSpec.from_claim(claim)
    grant = claim.get("artifact_upload") or {}
    v2_descriptors: Optional[list[dict[str, Any]]] = None
    is_private = spec.isolation_tier in ("sealed", "sealed_pro", "confidential")

    if is_private:
        try:
            result = _run_private_claim(
                base_url, machine_id, machine_secret, spec, claim, capabilities, executor_selector,
                transport=transport, timestamp=timestamp, timeout=timeout, attest_fn=attest_fn, hardware=hardware,
            )
        except KeyReleaseError as exc:
            if exc.job_closed:
                # The backend already failed (and refunded) the job; there is nothing to report.
                return {"claimed": True, "job_id": spec.job_id, "status": "failed", "error": exc.code}
            result = _failed_private(spec, exc.code, "the job's keys were not released to this host")
    elif spec.v2 is not None and isinstance(spec.v2.get("service"), dict):
        from .service_agent import run_service  # U11: a long-running service replica, not a batch run

        result = run_service(base_url, machine_id, machine_secret, spec, transport=transport)
    elif spec.v2 is not None and spec.v2.get("mode") == "vm":
        result, v2_descriptors = _run_vm(
            base_url, machine_id, machine_secret, spec, transport=transport, timestamp=timestamp, timeout=timeout,
        )
    elif spec.v2 is not None:
        result, v2_descriptors = _run_v2(
            base_url, machine_id, machine_secret, spec, capabilities, executor_selector,
            transport=transport, timestamp=timestamp, timeout=timeout,
        )
    else:
        result = _run_open(
            spec, capabilities, executor_selector,
            _make_monitor(base_url, machine_id, machine_secret, spec, transport),
        )

    # Stream logs (best-effort; a logging failure must not sink the receipt).
    # ADR-0005 & WP3 Checkpoint 3.2: Plaintext logs are dropped for private jobs.
    if result.logs and not is_private:
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
    descriptors: list[dict[str, Any]] = list(v2_descriptors or [])  # v2 outputs already went through the data plane
    if spec.v2 is None and grant.get("url") and grant.get("token"):
        for artifact in result.artifacts:
            descriptors.append(
                upload_artifact(grant, artifact, base_url=base_url, transport=transport, timeout=timeout)
            )

    body = build_result_body(result, descriptors)
    posted = post_result(
        base_url,
        machine_id,
        machine_secret,
        spec.job_id,
        body,
        transport=transport,
        timestamp=timestamp,
        timeout=timeout,
    )
    if getattr(posted, "status_code", 200) == 409:
        return {"claimed": True, "job_id": spec.job_id, "status": "cancelled", "result_refused": "job_not_running"}
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
    attestation_session: Optional[AttestationSession] = None,
    attestation_manager: Any = None,
    thermal_guard: Any = None,
    egress_proxy_image: Optional[str] = None,
    multinode_network: Optional[str] = None,
    multinode_network_firewalled: bool = False,
    cluster_addr: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Run the claim->execute->result loop a bounded number of times.

    ``thermal_guard`` (B6, a :class:`computecoin_agent.thermal.ThermalGuard`): while the GPU is at
    or above its cap no job is claimed (``{"claimed": False, "throttled": True}``) until it cools
    below the resume temperature; a running container is stopped at the hard stop.

    ``iterations <= 0`` loops until interrupted; a positive value runs exactly
    that many claim attempts (what tests use, with an injected ``transport`` and
    a no-op ``sleeper``). Transport/HTTP errors on a single iteration are caught
    so one bad claim never kills the loop. Returns the per-iteration summaries.
    """
    results: list[dict[str, Any]] = []
    i = 0
    extra: dict[str, Any] = {}
    # U13: one allocator shared by every job on this host so concurrent multi-GPU jobs get disjoint
    # --gpus "device=..." sets. Only NVIDIA hosts with more than one GPU need it; detection failure means none.
    gpu_allocator: Any = None
    try:
        from .gpu_alloc import GpuAllocator
        from .hardware import detect_gpus

        _gpus, _ = detect_gpus()
        if len(_gpus) > 1 and all(getattr(g, "gpu_vendor", "nvidia") == "nvidia" for g in _gpus):
            gpu_allocator = GpuAllocator(len(_gpus))
    except Exception:  # noqa: BLE001 - detection must never stop the claim loop
        gpu_allocator = None
    if thermal_guard is not None or egress_proxy_image is not None or multinode_network or gpu_allocator is not None:
        from .executor import DockerExecutor

        def _selector(spec: JobSpec, caps: Any = None) -> Executor:
            return select_executor(
                spec,
                caps,
                docker_executor_factory=lambda: DockerExecutor(
                    thermal_guard=thermal_guard, egress_proxy_image=egress_proxy_image,
                    multinode_network=multinode_network, multinode_network_firewalled=multinode_network_firewalled,
                    gpu_allocator=gpu_allocator, cluster_addr=cluster_addr,
                ),
            )

        extra["executor_selector"] = _selector
    while iterations <= 0 or i < iterations:
        try:
            if thermal_guard is not None and not thermal_guard.may_claim():
                summary = {"claimed": False, "throttled": True}
                results.append(summary)
                if emit is not None:
                    emit(i, summary)
                i += 1
                if iterations <= 0 or i < iterations:
                    sleeper(interval)
                continue
            summary = run_one(
                base_url,
                machine_id,
                machine_secret,
                transport=transport,
                capabilities=capabilities,
                timeout=timeout,
                attestation_session=attestation_manager.current() if attestation_manager is not None else attestation_session,
                hardware=getattr(attestation_manager, "hardware", None),
                **extra,
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
