"""Signed heartbeats.

Builds a canonical heartbeat payload and signs it with the machine's HMAC secret,
then POSTs it to the API. The transport is INJECTABLE so tests run fully offline.

A heartbeat tells the control plane: this machine is alive, here is its current
capability summary, and here is its health. The signature lets the control plane
reject spoofed or tampered reports (the capabilities themselves remain DETECTED /
untrusted until a server-side verification step runs).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Callable, Optional, Protocol

from .config import AgentConfig
from .hardware import CapabilityReport, build_capability_report

SIGNATURE_HEADER = "X-ComputeCoin-Signature"
MACHINE_HEADER = "X-ComputeCoin-Machine"
TIMESTAMP_HEADER = "X-ComputeCoin-Timestamp"

# Allowed clock skew for heartbeat timestamps (contract §heartbeat): ±300s.
MAX_SKEW_SECONDS = 300

# Process start, used to report a plausible uptime in the default health block.
_PROCESS_START = time.time()


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Deterministic JSON encoding used for signing and transmission."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_payload(payload: dict[str, Any], signing_secret: str) -> str:
    """HMAC-SHA256 hex signature over the canonical payload bytes."""
    key = signing_secret.encode("utf-8")
    return hmac.new(key, canonical_bytes(payload), hashlib.sha256).hexdigest()


def verify_signature(payload: dict[str, Any], signature: str, signing_secret: str) -> bool:
    """Constant-time verification (what the server side would do)."""
    expected = sign_payload(payload, signing_secret)
    return hmac.compare_digest(expected, signature)


def build_heartbeat_payload(
    machine_id: str,
    report: CapabilityReport,
    *,
    health: str = "healthy",
    timestamp: Optional[int] = None,
) -> dict[str, Any]:
    return {
        "machine_id": machine_id,
        "timestamp": int(timestamp if timestamp is not None else time.time()),
        "health": health,
        "capability_summary": report.summary(),
    }


# --------------------------------------------------------------------------- #
# Phase 2 contract: canonical signing string + heartbeat body
# --------------------------------------------------------------------------- #
class HeartbeatAuthError(RuntimeError):
    """Backend rejected the heartbeat (401): re-enrollment is required."""


class HeartbeatStaleError(RuntimeError):
    """Backend rejected the heartbeat as stale / replay (409 or skewed ts)."""


def contract_canonical_string(machine_id: str, timestamp: int, raw_body: bytes) -> str:
    """The EXACT canonical string signed per the Phase 2 contract.

    ``f"{machine_id}\\n{unix_timestamp}\\n{sha256_hex(raw_body_bytes)}"`` --
    ``\\n``-joined, no trailing newline. The body hash is over the RAW serialized
    request bytes (the exact bytes transmitted), never a re-serialization.
    """
    body_hash = hashlib.sha256(raw_body).hexdigest()
    return f"{machine_id}\n{int(timestamp)}\n{body_hash}"


_MACHINE_SECRET_PREFIX = "msk_"


def signing_key_from_secret(machine_secret: str) -> str:
    """Derive the HMAC signing key from a full ``msk_`` machine secret.

    Per the Phase 2 contract (docs/api/phase2-provider-protocol.md), the machine
    credential is ``msk_<signing_key>`` and the HMAC key is the ``signing_key`` --
    the component AFTER the ``msk_`` prefix. The server stores and keys by that
    stripped value, so the agent must sign with it too.
    """
    if machine_secret.startswith(_MACHINE_SECRET_PREFIX):
        return machine_secret[len(_MACHINE_SECRET_PREFIX):]
    return machine_secret


def sign_contract(
    machine_id: str, timestamp: int, raw_body: bytes, machine_secret: str
) -> str:
    """Hex HMAC-SHA256 over the contract canonical string, keyed by the signing
    key derived from ``machine_secret`` (the part after the ``msk_`` prefix)."""
    canonical = contract_canonical_string(machine_id, timestamp, raw_body)
    signing_key = signing_key_from_secret(machine_secret)
    return hmac.new(
        signing_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_contract_signature(
    machine_id: str,
    timestamp: int,
    raw_body: bytes,
    signature: str,
    machine_secret: str,
) -> bool:
    """Constant-time check mirroring the server-side verification."""
    expected = sign_contract(machine_id, timestamp, raw_body, machine_secret)
    return hmac.compare_digest(expected, signature)


def default_health(report: Optional[CapabilityReport] = None) -> dict[str, Any]:
    """Minimal health block matching the contract's ``health`` shape."""
    return {
        "gpu_temp_c": None,
        "gpu_util_pct": None,
        "mem_used_mb": None,
        "uptime_s": int(max(0.0, time.time() - _PROCESS_START)),
    }


def build_contract_heartbeat_body(
    report: CapabilityReport,
    *,
    availability: str = "online",
    health: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build the Phase 2 heartbeat body:
    ``{availability, capability_summary:{gpu_count,vram_mb}, health:{...}}``.
    """
    return {
        "availability": availability,
        "capability_summary": report.contract_summary(),
        "health": health if health is not None else default_health(report),
    }


def _client(transport: Any, timeout: float):
    import httpx

    if transport is not None:
        return httpx.Client(transport=transport, timeout=timeout)
    return httpx.Client(timeout=timeout)


def send_contract_heartbeat(
    base_url: str,
    machine_id: str,
    machine_secret: str,
    *,
    report: Optional[CapabilityReport] = None,
    availability: str = "online",
    health: Optional[dict[str, Any]] = None,
    transport: Any = None,
    timestamp: Optional[int] = None,
    report_factory: Callable[[], CapabilityReport] = build_capability_report,
    timeout: float = 10.0,
) -> Any:
    """Build, sign and POST one heartbeat per the Phase 2 contract.

    The body is serialized ONCE; those exact bytes are both signed (via the
    canonical string over their sha256) and transmitted -- never re-serialized.
    Headers carry the machine id, unix-seconds timestamp, and hex HMAC-SHA256
    signature. Raises :class:`HeartbeatAuthError` on 401 (re-enroll) and
    :class:`HeartbeatStaleError` on 409 / stale-timestamp rejection. ``transport``
    is injectable (e.g. ``httpx.MockTransport``) for offline tests.
    """
    if not machine_id or not machine_secret:
        raise RuntimeError(
            "machine is not enrolled; run `computecoin-agent enroll` first"
        )

    report = report or report_factory()
    body = build_contract_heartbeat_body(
        report, availability=availability, health=health
    )
    raw = canonical_bytes(body)  # serialize ONCE
    ts = int(timestamp if timestamp is not None else time.time())
    signature = sign_contract(machine_id, ts, raw, machine_secret)
    headers = {
        "Content-Type": "application/json",
        MACHINE_HEADER: machine_id,
        TIMESTAMP_HEADER: str(ts),
        SIGNATURE_HEADER: signature,
    }
    url = base_url.rstrip("/") + "/v1/agents/heartbeat"
    with _client(transport, timeout) as client:
        resp = client.post(url, content=raw, headers=headers)  # send EXACT bytes

    if resp.status_code == 401:
        raise HeartbeatAuthError(
            "heartbeat rejected (401): unknown machine, bad signature, or "
            "deregistered -- re-enrollment required"
        )
    if resp.status_code == 409:
        raise HeartbeatStaleError(
            "heartbeat rejected (409): timestamp outside allowed skew / replay"
        )
    return resp


def heartbeat_loop(
    base_url: str,
    machine_id: str,
    machine_secret: str,
    *,
    report: Optional[CapabilityReport] = None,
    iterations: int = 0,
    interval: float = 30.0,
    availability: str = "online",
    transport: Any = None,
    report_factory: Callable[[], CapabilityReport] = build_capability_report,
    sleeper: Callable[[float], None] = time.sleep,
    emit: Optional[Callable[[int, Any], None]] = None,
) -> list[Any]:
    """Send heartbeats on a fixed cadence.

    ``iterations <= 0`` loops until interrupted; a positive value runs a bounded
    number of heartbeats (what tests use, with an injected ``transport`` and a
    no-op ``sleeper``). Returns the list of responses. Raises
    :class:`HeartbeatAuthError` immediately (re-enrollment needed); treats
    :class:`HeartbeatStaleError` and transient transport errors as non-fatal.
    """
    report = report or report_factory()
    responses: list[Any] = []
    i = 0
    while iterations <= 0 or i < iterations:
        try:
            resp = send_contract_heartbeat(
                base_url,
                machine_id,
                machine_secret,
                report=report,
                availability=availability,
                transport=transport,
            )
            responses.append(resp)
            if emit is not None:
                emit(i, resp)
        except HeartbeatAuthError:
            raise  # fatal: credential is no longer valid
        except HeartbeatStaleError as exc:
            if emit is not None:
                emit(i, exc)
        except Exception as exc:  # transient network error: keep going
            if emit is not None:
                emit(i, exc)
        i += 1
        if iterations <= 0 or i < iterations:
            sleeper(interval)
    return responses


class Transport(Protocol):
    """Minimal transport interface so the network call is injectable."""

    def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> "TransportResponse":
        ...


class TransportResponse(Protocol):
    status_code: int

    def json(self) -> Any:
        ...


class HttpxTransport:
    """Default transport backed by httpx. Imported lazily so offline tests that
    inject their own transport never need httpx at call time."""

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def post(self, url: str, *, content: bytes, headers: dict[str, str]):
        import httpx

        return httpx.post(url, content=content, headers=headers, timeout=self._timeout)


def send_heartbeat(
    config: AgentConfig,
    *,
    report: Optional[CapabilityReport] = None,
    health: str = "healthy",
    transport: Optional[Transport] = None,
    report_factory: Callable[[], CapabilityReport] = build_capability_report,
    timestamp: Optional[int] = None,
) -> TransportResponse:
    """Build, sign and POST a single heartbeat.

    ``transport`` defaults to httpx but can be injected in tests. Raises
    ``RuntimeError`` if the machine is not enrolled.
    """
    if not config.machine_id or not config.signing_secret:
        raise RuntimeError("machine is not enrolled; run `computecoin-agent enroll` first")

    report = report or report_factory()
    payload = build_heartbeat_payload(
        config.machine_id, report, health=health, timestamp=timestamp
    )
    signature = sign_payload(payload, config.signing_secret)
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature,
        MACHINE_HEADER: config.machine_id,
    }
    tx = transport or HttpxTransport()
    url = config.api_url.rstrip("/") + "/v1/agents/heartbeat"
    return tx.post(url, content=canonical_bytes(payload), headers=headers)
