"""Security primitives: password hashing, opaque-token hashing, JWT handling."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import settings

# Argon2id is the default variant for argon2-cffi's PasswordHasher.
_password_hasher = PasswordHasher()


# ---------------------------------------------------------------------------
# Password hashing (Argon2id)
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


_dummy_hash: str | None = None


def burn_password_check(password: str) -> None:
    """Spend the same time a real verification would, for a login whose account does not exist.

    Without it an unknown email answers in microseconds and a known one after a full Argon2 verify, which tells an
    attacker which addresses are registered even though the error message is identical."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _password_hasher.hash(secrets.token_urlsafe(16))
    verify_password(_dummy_hash, password)


def needs_rehash(password_hash: str) -> bool:
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Opaque tokens (refresh tokens, verification/reset tokens, api keys)
# Stored only as a hash. We use SHA-256 for constant-time lookupable hashing.
# ---------------------------------------------------------------------------
def generate_opaque_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """SHA-256 hash of an opaque token, suitable for DB lookup by hash."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    # Bytes, not str: compare_digest raises TypeError on non-ASCII str (a 500 for attacker-chosen input).
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------
#: Marks a bearer credential as a project API key rather than a JWT. The auth
#: dependency branches on this, so it must stay in sync with generate_api_key.
API_KEY_PREFIX = "cc_live_"


def generate_api_key() -> tuple[str, str, str]:
    """Return (full_key, prefix, key_hash).

    full_key looks like ``cc_live_<random>``. Only prefix + hash are stored.
    """
    random_part = secrets.token_urlsafe(32)
    full_key = f"{API_KEY_PREFIX}{random_part}"
    # prefix shown to user: scheme + first chars of the random body
    prefix = f"{API_KEY_PREFIX}{random_part[:8]}"
    key_hash = hash_token(full_key)
    return full_key, prefix, key_hash


# ---------------------------------------------------------------------------
# JWT access tokens
# ---------------------------------------------------------------------------
def create_access_token(
    *,
    subject: str,
    roles: list[str],
    expires_in: int | None = None,
    jti: str | None = None,
) -> tuple[str, str, dt.datetime]:
    """Create a signed access JWT.

    Returns (token, jti, expires_at).
    """
    now = dt.datetime.now(dt.timezone.utc)
    ttl = expires_in if expires_in is not None else settings.ACCESS_TOKEN_TTL_SECONDS
    exp = now + dt.timedelta(seconds=ttl)
    token_jti = jti or str(uuid.uuid4())
    payload: dict[str, Any] = {
        "sub": subject,
        "roles": roles,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": token_jti,
        "iss": settings.JWT_ISSUER,
    }
    token = jwt.encode(payload, settings.AUTH_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, token_jti, exp


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode & validate a JWT. Raises jwt exceptions on failure."""
    return jwt.decode(
        token,
        settings.AUTH_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
        issuer=settings.JWT_ISSUER,
        options={"require": ["exp", "sub", "jti"]},
    )
