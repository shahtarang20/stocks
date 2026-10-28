"""Freshness checks for attestation challenge nonces.

Both attestation routes (provider login: ``/provider/machines/{id}/attestation/*``; machine HMAC:
``/v1/agents/attestation/*``) store the nonce and its creation time in ``machine.last_health``.
They must refuse a nonce the same way, so the check lives here once instead of in two copies
that can drift apart.
"""

from __future__ import annotations

import datetime as dt
import hmac
from typing import Any

from fastapi import HTTPException, status

from app.schemas.attestation import AttestationChallengeResponse

NONCE_KEY = "latest_attestation_nonce"
NONCE_CREATED_AT_KEY = "latest_attestation_nonce_created_at"
# Set when the challenge was issued for ONE private job (WP-B): the verdict is then bound to it.
NONCE_JOB_KEY = "latest_attestation_nonce_job_id"

# The challenge response promises this lifetime to the caller, so the verify side enforces the
# same number rather than a second constant that could disagree with it.
NONCE_MAX_AGE_SECONDS: int = AttestationChallengeResponse.model_fields["expires_in_seconds"].default


def _invalid_nonce() -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_or_expired_nonce")


def issue_attestation_nonce(
    health: dict[str, Any], nonce: str, *, now: dt.datetime, job_id: str | None = None
) -> dict[str, Any]:
    """Return ``health`` with ``nonce`` stored as the machine's one open challenge.

    ``job_id`` binds the challenge to one private job; a challenge without it is a plain
    machine attestation and can never release keys.
    """
    updated = dict(health)
    updated[NONCE_KEY] = nonce
    updated[NONCE_CREATED_AT_KEY] = now.isoformat()
    if job_id:
        updated[NONCE_JOB_KEY] = job_id
    else:
        updated.pop(NONCE_JOB_KEY, None)
    return updated


def nonce_job_id(health: dict[str, Any]) -> str | None:
    """The job the open challenge was issued for, if any."""
    value = health.get(NONCE_JOB_KEY)
    return value if isinstance(value, str) and value else None


def require_fresh_attestation_nonce(health: dict[str, Any], nonce: str, *, now: dt.datetime) -> None:
    """Refuse ``nonce`` unless it is the stored one and younger than ``NONCE_MAX_AGE_SECONDS``.

    Fails closed: a missing or unparseable creation time is treated as expired, because a nonce
    with no known age could be arbitrarily old. The compare is constant-time.
    """
    stored = health.get(NONCE_KEY)
    # Compare bytes: compare_digest raises TypeError on non-ASCII str, which would be a 500.
    if not isinstance(stored, str) or not stored or not hmac.compare_digest(
        stored.encode("utf-8"), nonce.encode("utf-8")
    ):
        raise _invalid_nonce()
    raw_created = health.get(NONCE_CREATED_AT_KEY)
    if not isinstance(raw_created, str):
        raise _invalid_nonce()
    try:
        created = dt.datetime.fromisoformat(raw_created)
    except ValueError:
        raise _invalid_nonce() from None
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    age = (now - created).total_seconds()
    # A creation time in the future means a clock problem or a forged value; refuse it too.
    if age < 0 or age > NONCE_MAX_AGE_SECONDS:
        raise _invalid_nonce()


def consume_attestation_nonce(health: dict[str, Any]) -> dict[str, Any]:
    """Return ``health`` with the open challenge removed so it cannot be replayed."""
    updated = dict(health)
    updated.pop(NONCE_KEY, None)
    updated.pop(NONCE_CREATED_AT_KEY, None)
    updated.pop(NONCE_JOB_KEY, None)
    return updated
