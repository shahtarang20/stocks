"""FastAPI dependencies: DB session, current user, RBAC."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.enums import UserStatus
from app.models.user import User
from app.schemas.auth import CurrentUser
from app.services.auth import get_user_by_id, get_user_roles

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


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    if credentials is None or not credentials.credentials:
        raise _CREDENTIALS_ERROR
    try:
        payload = decode_access_token(credentials.credentials)
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
