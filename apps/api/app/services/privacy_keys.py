"""Key material for Proof-of-Privacy (ADR-0005), read from settings.

Kept in one place so the verifier key, the guardian keys and the "is this feature on" rule are
decided once and every caller agrees.
"""

from __future__ import annotations

import json

from app.core.config import settings
from app.services.privacy_crypto import ed25519_public_from_private, is_hex32, x25519_public_from_private


class PrivacyConfigError(RuntimeError):
    """Proof-of-Privacy is off, or its keys are missing or malformed."""


def require_privacy_active() -> None:
    if not settings.privacy_active:
        raise PrivacyConfigError("privacy_tiers_not_enabled")


def _json_map(raw: str, what: str) -> dict[str, str]:
    try:
        data = json.loads(raw or "{}")
    except ValueError as exc:
        raise PrivacyConfigError(f"{what} is not valid JSON") from exc
    if not isinstance(data, dict) or not all(isinstance(k, str) and is_hex32(v) for k, v in data.items()):
        raise PrivacyConfigError(f"{what} must map guardian names to 32-byte hex keys")
    return {k: v.lower() for k, v in data.items()}


def guardian_private_keys() -> dict[str, str]:
    """Guardians whose private key is held by THIS process."""
    return _json_map(settings.PRIVACY_GUARDIAN_PRIVATE_KEYS, "PRIVACY_GUARDIAN_PRIVATE_KEYS")


def guardian_public_keys() -> dict[str, str]:
    """Every guardian, local or external, in a stable order (sorted by name)."""
    explicit = _json_map(settings.PRIVACY_GUARDIAN_PUBLIC_KEYS, "PRIVACY_GUARDIAN_PUBLIC_KEYS")
    derived = {name: x25519_public_from_private(priv) for name, priv in guardian_private_keys().items()}
    for name, pub in derived.items():
        if name in explicit and explicit[name] != pub:
            raise PrivacyConfigError(f"guardian {name}: public key does not match its private key")
    merged = {**derived, **explicit}
    return dict(sorted(merged.items()))


def guardian_threshold() -> int:
    k = settings.PRIVACY_GUARDIAN_THRESHOLD
    if k < 2:
        raise PrivacyConfigError("PRIVACY_GUARDIAN_THRESHOLD must be at least 2")
    return k


def verifier_signing_key() -> str:
    key = settings.PRIVACY_VERIFIER_SIGNING_KEY
    if not is_hex32(key):
        raise PrivacyConfigError("PRIVACY_VERIFIER_SIGNING_KEY must be a 32-byte hex Ed25519 seed")
    return key


def verifier_public_key() -> str:
    return ed25519_public_from_private(verifier_signing_key())
