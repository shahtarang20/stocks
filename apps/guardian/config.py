"""Configuration for the external guardian service. Read from the environment; nothing has a usable
default, because a guardian that is missing any of these must refuse to release (fail closed)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _set_from_env(name: str) -> set[str]:
    return {item.strip().lower() for item in os.getenv(name, "").split(",") if item.strip()}


def _dict_from_env(name: str) -> dict[str, str]:
    raw = os.getenv(name, "").strip()
    if not raw or raw == "{}":
        return {}
    try:
        import json
        parsed = json.loads(raw)
        return {str(k).strip(): str(v).strip().lower() for k, v in parsed.items()} if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _is_hex32(value: str) -> bool:
    try:
        return len(bytes.fromhex(value)) == 32
    except ValueError:
        return False


@dataclass
class GuardianConfig:
    name: str = field(default_factory=lambda: os.getenv("GUARDIAN_NAME", ""))
    # This guardian's X25519 private key (hex). Customers seal one Data Key share to its public key.
    private_key: str = field(default_factory=lambda: os.getenv("GUARDIAN_PRIVATE_KEY", ""))
    # Pinned Ed25519 keys of the platform: the attestation verifier and the transparency log.
    verifier_public_key: str = field(default_factory=lambda: os.getenv("VERIFIER_PUBLIC_KEY", "").lower())
    log_public_key: str = field(default_factory=lambda: os.getenv("LOG_PUBLIC_KEY", "").lower())
    # OS image hashes this guardian itself trusts. Empty = trust nothing.
    trusted_images: set[str] = field(default_factory=lambda: _set_from_env("TRUSTED_IMAGES"))
    # Where the guardian remembers the largest log it has seen and the releases it made.
    state_path: str = field(default_factory=lambda: os.getenv("GUARDIAN_STATE_PATH", "/var/lib/guardian/state.json"))
    # A job may be released again only to a NEW verdict (a retry on another host), at most this often.
    max_releases_per_job: int = field(default_factory=lambda: int(os.getenv("GUARDIAN_MAX_RELEASES_PER_JOB", "3")))
    # Pinned Ed25519 public keys of independent log witnesses: JSON {name: pubkey_hex}.
    witness_keys: dict[str, str] = field(default_factory=lambda: _dict_from_env("GUARDIAN_WITNESS_KEYS"))
    # Minimum valid witness cosignatures required on a checkpoint before releasing (WP-E).
    witness_threshold: int = field(default_factory=lambda: int(os.getenv("GUARDIAN_WITNESS_THRESHOLD", "0")))

    def missing(self) -> list[str]:
        out = []
        if not self.name:
            out.append("GUARDIAN_NAME")
        for attr, env in (("private_key", "GUARDIAN_PRIVATE_KEY"), ("verifier_public_key", "VERIFIER_PUBLIC_KEY"), ("log_public_key", "LOG_PUBLIC_KEY")):
            value = getattr(self, attr)
            try:
                if len(bytes.fromhex(value)) != 32:
                    out.append(env)
            except ValueError:
                out.append(env)
        if not self.trusted_images:
            out.append("TRUSTED_IMAGES")
        if self.witness_threshold > 0:
            keys = set(self.witness_keys.values())
            if self.witness_threshold > len(keys) or any(not _is_hex32(k) for k in keys):
                # A threshold that can never be met (or unusable keys) would refuse every release
                # while /health said "ok"; report it instead.
                out.append("GUARDIAN_WITNESS_KEYS")
        return out
