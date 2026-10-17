"""Machine enrollment.

Pairs a provider's machine to their ComputeCoin account using a revocable,
MACHINE-SCOPED enrollment token. On success we:

  * derive a stable ``machine_id``,
  * generate a per-machine HMAC signing secret (used to sign heartbeats), and
  * persist both to the 0600 config file.

TRUST MODEL
-----------
The enrollment token is issued server-side, bound to one machine, and revocable.
It is a bearer secret only during the enroll call; afterwards the machine
authenticates with its own signing secret, so revoking/rotating the token does
not require re-keying the machine. The signing secret never leaves the host in
cleartext -- only HMAC signatures derived from it are transmitted.

We use an HMAC (symmetric shared secret) scheme for the skeleton because it has
zero native dependencies and is trivial to verify in tests. A later phase can
upgrade to an asymmetric keypair (Ed25519 via ``cryptography``) so the control
plane never holds the signing key; the heartbeat module is written so that only
the signing function changes.
"""

from __future__ import annotations

import platform
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from . import __version__
from .config import AgentConfig
from .hardware import CapabilityReport


class EnrollmentError(RuntimeError):
    """Raised when the backend rejects an enrollment (e.g. token invalid)."""


@dataclass
class EnrollmentResult:
    machine_id: str
    provider_id: Optional[str]
    config_path: Path


@dataclass
class RemoteEnrollmentResult:
    """Result of a successful backend enrollment (Phase 2)."""

    machine_id: str
    machine_secret: str
    heartbeat_interval_seconds: int
    online_window_seconds: int
    config_path: Path


def _derive_machine_id(token: str) -> str:
    """Derive a machine id.

    In the skeleton we generate a fresh random id at enroll time. A production
    agent would bind this to stable hardware identifiers (e.g. a hash of
    DMI/board serial + MAC) so re-enrollment maps to the same machine. We keep it
    random here to avoid reading host identifiers in a CPU-only test sandbox.
    """
    return "mch_" + secrets.token_hex(8)


def generate_signing_secret() -> str:
    """Generate a 256-bit HMAC signing secret, hex-encoded."""
    return secrets.token_hex(32)


def enroll(
    token: str,
    *,
    config: Optional[AgentConfig] = None,
    config_path: Optional[Path] = None,
    provider_id: Optional[str] = None,
    machine_id: Optional[str] = None,
) -> EnrollmentResult:
    """Enroll this machine and persist credentials with 0600 perms.

    ``token`` is the machine-scoped enrollment token. In this skeleton we do not
    call the server to exchange it (offline-friendly); we record it and mint a
    local signing secret. The real agent would POST the token + capability report
    to ``/v1/agents/enroll`` and receive back the server-assigned machine_id.
    """
    if not token or not token.strip():
        raise ValueError("enrollment token must be a non-empty string")

    cfg = config or AgentConfig.load(config_path)
    cfg.enrollment_token = token.strip()
    cfg.machine_id = machine_id or _derive_machine_id(token)
    cfg.provider_id = provider_id or cfg.provider_id
    cfg.signing_secret = generate_signing_secret()

    saved_path = cfg.save(config_path)
    return EnrollmentResult(
        machine_id=cfg.machine_id,
        provider_id=cfg.provider_id,
        config_path=saved_path,
    )


def _client(transport, timeout: float):
    """Build an httpx client, optionally over an injected transport.

    ``transport`` may be an ``httpx.MockTransport`` (or any ``httpx`` transport)
    so tests run fully offline; when ``None`` a real networked client is used.
    """
    import httpx

    if transport is not None:
        return httpx.Client(transport=transport, timeout=timeout)
    return httpx.Client(timeout=timeout)


def enroll_remote(
    base_url: str,
    enrollment_token: str,
    capability_report: Union[CapabilityReport, dict[str, Any]],
    transport: Any = None,
    *,
    config_path: Optional[Path] = None,
    agent_version: str = __version__,
    hostname: Optional[str] = None,
    supported_workloads: Optional[list[str]] = None,
    timeout: float = 10.0,
) -> RemoteEnrollmentResult:
    """Enroll this machine against the backend per the Phase 2 contract.

    POSTs to ``{base_url}/v1/agents/enroll`` with the enrollment token and a
    ``capability_report`` (``verification_status="detected"``, ``gpus=[]`` on a
    CPU-only host). On ``201`` the returned ``machine_id`` + ``machine_secret``
    (``msk_...``) are persisted to the 0600 agent config. On ``401`` an
    :class:`EnrollmentError` is raised and NOTHING is written to disk.

    ``capability_report`` may be a :class:`CapabilityReport` (mapped to the
    contract shape here) or an already contract-shaped dict. ``transport`` is
    injectable (e.g. ``httpx.MockTransport``) for offline tests.
    """
    if not enrollment_token or not enrollment_token.strip():
        raise EnrollmentError("enrollment token must be a non-empty string")

    if isinstance(capability_report, CapabilityReport):
        cap = capability_report.contract_capability_report(
            supported_workloads=supported_workloads
        )
        hostname = hostname or capability_report.hostname
    else:
        cap = dict(capability_report)
        # Enforce the honesty rule regardless of what the caller passed.
        cap["verification_status"] = "detected"

    hostname = hostname or platform.node() or "unknown"

    body = {
        "enrollment_token": enrollment_token.strip(),
        "agent_version": agent_version,
        "hostname": hostname,
        "capability_report": cap,
    }

    url = base_url.rstrip("/") + "/v1/agents/enroll"
    with _client(transport, timeout) as client:
        resp = client.post(url, json=body)

    if resp.status_code == 401:
        raise EnrollmentError(
            "enrollment rejected (401): token invalid, expired, already used, or revoked"
        )
    if resp.status_code != 201:
        detail = ""
        try:
            detail = f": {resp.text}"
        except Exception:
            pass
        raise EnrollmentError(
            f"enrollment failed: HTTP {resp.status_code}{detail}"
        )

    data = resp.json()
    try:
        machine_id = data["machine_id"]
        machine_secret = data["machine_secret"]
    except (KeyError, TypeError) as exc:
        raise EnrollmentError(f"enrollment response missing credentials: {exc}") from exc

    heartbeat_interval = int(data.get("heartbeat_interval_seconds", 30))
    online_window = int(data.get("online_window_seconds", 90))

    # Only now -- after a confirmed 201 -- do we touch disk.
    cfg = AgentConfig.load(config_path)
    cfg.api_url = base_url
    cfg.machine_id = machine_id
    cfg.machine_secret = machine_secret
    cfg.heartbeat_interval_seconds = heartbeat_interval
    cfg.online_window_seconds = online_window
    saved_path = cfg.save(config_path)

    return RemoteEnrollmentResult(
        machine_id=machine_id,
        machine_secret=machine_secret,
        heartbeat_interval_seconds=heartbeat_interval,
        online_window_seconds=online_window,
        config_path=saved_path,
    )
