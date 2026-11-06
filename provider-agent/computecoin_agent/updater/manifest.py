"""Signed update manifest (cross-platform plan W6, X10): format, strict validation, Ed25519 verification.

Wire format: ONE flat JSON object. ``signature`` is the lower-case hex of an Ed25519 signature over
``SIGN_PREFIX + canonical_bytes(object without "signature")``; nothing outside the signed bytes is trusted (the signature
is checked BEFORE any field is read).

====================  ============================================================================================
``schema``            1
``product``           ``"computecoin-agent"``
``version``           the version this artifact installs
``platform``          ``"windows"`` | ``"linux"``
``artifact_url``      https URL of the installer package
``sha256``            hex SHA-256 of the artifact bytes
``size_bytes``        artifact size (download cap)
``min_supported``     the FORCED-UPDATE FLOOR: a host below it must update and claims no work until it has
``ring``              ``"canary"`` | ``"early"`` | ``"general"``: the widest ring this release is offered to
``issued_at``         unix seconds
``expires_at``        optional unix seconds; a manifest after it is refused
``revoked_versions``  optional: versions that must be replaced even by an OLDER release (this is how a rollback to the
                      previous release is authorised: a downgrade is refused unless the running version is listed here)
====================  ============================================================================================

The public key is PINNED in the agent build (``PINNED_PUBLIC_KEYS``); the private key never exists in this repository (L7).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from .version import compare, is_valid_version

SIGN_PREFIX = b"computecoin-agent-update-v1\n"
PRODUCT = "computecoin-agent"
RINGS = ("canary", "early", "general")  # narrowest to widest
PLATFORMS = ("windows", "linux")
MAX_ARTIFACT_BYTES = 1024**3  # an agent installer is tens of MB; refuse anything absurd
MAX_REVOKED = 64

# Ed25519 public keys (64 hex chars each) the agent accepts. EMPTY until the owner's signing key ceremony: with no
# pinned key every manifest is refused (fail closed, L1). Two entries during a key rotation.
PINNED_PUBLIC_KEYS: tuple = ()

_REQUIRED = {"schema", "product", "version", "platform", "artifact_url", "sha256", "size_bytes", "min_supported", "ring", "issued_at"}
_OPTIONAL = {"expires_at", "revoked_versions"}
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SIG_RE = re.compile(r"^[0-9a-f]{128}$")
_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ManifestError(Exception):
    """``code`` is stable and safe to log."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def signing_input(payload: Mapping[str, Any]) -> bytes:
    return SIGN_PREFIX + canonical_bytes(payload)


@dataclasses.dataclass(frozen=True)
class UpdateManifest:
    version: str
    platform: str
    artifact_url: str
    sha256: str
    size_bytes: int
    min_supported: str
    ring: str
    issued_at: int
    expires_at: Optional[int]
    revoked_versions: tuple
    signature: str

    def authorises_downgrade_from(self, running_version: str) -> bool:
        return running_version in self.revoked_versions


def ring_rank(ring: str) -> int:
    return RINGS.index(ring)


def verify_signature(raw: Mapping[str, Any], public_keys: Sequence[str]) -> None:
    """Raises ManifestError('no_pinned_key' | 'malformed' | 'signature_invalid'). Checks nothing else."""
    keys = [k for k in public_keys if isinstance(k, str) and _KEY_RE.match(k)]
    if not keys:
        raise ManifestError("no_pinned_key", "no update signing key is pinned in this build")
    sig = raw.get("signature") if isinstance(raw, Mapping) else None
    if not isinstance(sig, str) or not _SIG_RE.match(sig):
        raise ManifestError("malformed", "signature missing or not 128 hex characters")
    payload = {k: v for k, v in raw.items() if k != "signature"}
    try:
        data = signing_input(payload)
    except (TypeError, ValueError):
        raise ManifestError("malformed", "manifest is not serialisable")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    sig_bytes = bytes.fromhex(sig)
    for key_hex in keys:
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex)).verify(sig_bytes, data)
            return
        except InvalidSignature:
            continue
    raise ManifestError("signature_invalid", "no pinned key verifies this manifest")


def _check_url(url: Any, allowed_hosts: Optional[frozenset]) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ManifestError("bad_url", "artifact_url missing")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ManifestError("bad_url", "artifact_url must be https")
    if parts.username or parts.password:
        raise ManifestError("bad_url", "artifact_url must not carry credentials")
    if allowed_hosts is not None and parts.hostname.lower() not in allowed_hosts:
        raise ManifestError("bad_url", "artifact host is not allowed")
    return url


def parse_manifest(
    raw: Mapping[str, Any],
    public_keys: Sequence[str],
    *,
    platform: str,
    host_ring: str,
    now: float,
    allowed_hosts: Optional[frozenset] = None,
) -> UpdateManifest:
    """Verify the signature FIRST, then validate every field and the platform and ring this host expects.

    ``host_ring`` is the ring this host belongs to; the server decides which manifest it gets, and the agent only checks
    that the answer is not for a NARROWER ring than its own (a general host never takes a canary build).
    """
    if not isinstance(raw, Mapping):
        raise ManifestError("malformed", "manifest is not an object")
    verify_signature(raw, public_keys)
    keys = set(raw) - {"signature"}
    if keys - _REQUIRED - _OPTIONAL or _REQUIRED - keys:
        raise ManifestError("malformed", "missing or unknown fields")
    if raw["schema"] != 1 or raw["product"] != PRODUCT:
        raise ManifestError("malformed", "unsupported schema or product")
    version, floor = raw["version"], raw["min_supported"]
    if not is_valid_version(version) or not is_valid_version(floor):
        raise ManifestError("malformed", "bad version")
    if compare(floor, version) > 0:
        raise ManifestError("malformed", "min_supported is above version")
    if raw["platform"] not in PLATFORMS:
        raise ManifestError("malformed", "unknown platform")
    if raw["platform"] != platform:
        raise ManifestError("platform_mismatch", "manifest is for another platform")
    if raw["ring"] not in RINGS or host_ring not in RINGS:
        raise ManifestError("malformed", "unknown ring")
    if ring_rank(host_ring) > ring_rank(raw["ring"]):
        raise ManifestError("ring_mismatch", "manifest is for a narrower ring than this host's")
    sha = raw["sha256"]
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise ManifestError("malformed", "sha256 must be 64 lower-case hex characters")
    size = raw["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ARTIFACT_BYTES:
        raise ManifestError("malformed", "bad size_bytes")
    issued = raw["issued_at"]
    if isinstance(issued, bool) or not isinstance(issued, int) or issued < 0:
        raise ManifestError("malformed", "bad issued_at")
    expires = raw.get("expires_at")
    if expires is not None:
        if isinstance(expires, bool) or not isinstance(expires, int) or expires <= issued:
            raise ManifestError("malformed", "bad expires_at")
        if now >= expires:
            raise ManifestError("expired", "manifest has expired")
    revoked = raw.get("revoked_versions", [])
    if not isinstance(revoked, list) or len(revoked) > MAX_REVOKED or not all(is_valid_version(v) for v in revoked):
        raise ManifestError("malformed", "bad revoked_versions")
    return UpdateManifest(
        version=version,
        platform=raw["platform"],
        artifact_url=_check_url(raw["artifact_url"], allowed_hosts),
        sha256=sha,
        size_bytes=size,
        min_supported=floor,
        ring=raw["ring"],
        issued_at=issued,
        expires_at=expires,
        revoked_versions=tuple(revoked),
        signature=raw["signature"],
    )


def sha256_file(path: Any, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()
