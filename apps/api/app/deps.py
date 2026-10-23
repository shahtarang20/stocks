"""FastAPI dependencies: DB session, current user, RBAC."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.core.security import (
    API_KEY_PREFIX,
    decode_access_token,
    hash_token,
)
from app.db.base import utcnow
from app.db.session import get_db
from app.models.enums import UserStatus
from app.models.user import User
from app.schemas.auth import CurrentUser
from app.services.auth import get_user_by_id, get_user_roles

import secrets

CSRF_PROTECTED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

bearer_scheme = HTTPBearer(auto_error=False)

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


async def _authenticate_api_key(db: AsyncSession, raw_key: str) -> CurrentUser:
    """Resolve a ``cc_live_`` project API key to the project's owner.

    Only the SHA-256 of the key is stored, so the lookup is an exact match on an
    indexed hash column: the plaintext is never compared and never recoverable
    from the database.

    A key grants exactly what its project's owner can do. It is refused when the
    key is revoked, when the project is gone, or when the owner is not active —
    so suspending an account also kills every key it issued, rather than leaving
    a live credential behind.
    """
    from app.models.org import ApiKey, Project

    key_hash = hash_token(raw_key)
    api_key = await db.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash))
    if api_key is None or api_key.revoked_at is not None:
        raise _CREDENTIALS_ERROR

    project = await db.scalar(select(Project).where(Project.id == api_key.project_id))
    if project is None:
        raise _CREDENTIALS_ERROR

    user: User | None = await get_user_by_id(db, project.owner_id)
    if user is None or user.status != UserStatus.active:
        raise _CREDENTIALS_ERROR

    # Last-used is what makes an unused or leaked key visible to its owner.
    api_key.last_used_at = utcnow()

    roles = await get_user_roles(db, user.id)
    return CurrentUser(
        id=user.id,
        email=user.email,
        email_verified=user.email_verified,
        status=user.status.value,
        roles=roles,
    )


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    token: str | None = None
    is_cookie_auth = False

    # 1. Explicit Bearer header takes precedence (SDKs, CLI, agent workers, integrations)
    if credentials is not None and credentials.credentials:
        token = credentials.credentials
    # 2. httpOnly cookie auth for web browsers
    elif "access_token" in request.cookies:
        token = request.cookies.get("access_token")
        is_cookie_auth = True

    if not token:
        raise _CREDENTIALS_ERROR

    # 3. Double-submit CSRF token validation on mutating methods when using cookie auth
    if is_cookie_auth and request.method.upper() in CSRF_PROTECTED_METHODS:
        csrf_cookie = request.cookies.get("csrf_token")
        csrf_header = request.headers.get("x-csrf-token")
        if not csrf_cookie or not csrf_header or not secrets.compare_digest(csrf_cookie, csrf_header):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed: missing or invalid X-CSRF-Token header",
            )

    # 4. Project API keys. These are issued by POST /projects/{id}/api-keys and
    # are the documented way to call the API from code, so they must actually
    # authenticate — previously nothing consumed them and every key 401'd.
    # They are distinguishable from a JWT by their prefix, so this branch can
    # never shadow a token.
    if token.startswith(API_KEY_PREFIX):
        return await _authenticate_api_key(db, token)

    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        raise _CREDENTIALS_ERROR

    sub = payload.get("sub")
    if not sub:
        raise _CREDENTIALS_ERROR
    try:
        user_id = uuid.UUID(sub)
    except (ValueError, TypeError):
        raise _CREDENTIALS_ERROR

    user: User | None = await get_user_by_id(db, user_id)
    if user is None or user.status != UserStatus.active:
        raise _CREDENTIALS_ERROR

    roles = await get_user_roles(db, user.id)
    return CurrentUser(
        id=user.id,
        email=user.email,
        email_verified=user.email_verified,
        status=user.status.value,
        roles=roles,
    )


async def require_verified_user(
    current: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Require the caller's email to be verified before sensitive actions
    (spending credits, running compute). Blocks unverified/throwaway accounts
    from abusing money/compute endpoints while still allowing read access."""
    if not current.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email_not_verified",
        )
    return current


def require_roles(*required: str) -> Callable:
    """Return a dependency that enforces the caller has at least one role."""
    required_set = set(required)

    async def _dependency(
        current: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if required_set and not (required_set & set(current.roles)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return current

    return _dependency
