"""Auth service: user lookup, session issuance, refresh rotation + reuse detection."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token,
    generate_opaque_token,
    hash_token,
)
from app.db.base import utcnow
from app.models.user import User, UserRole, UserSession


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    normalized = email.strip().lower()
    result = await db.execute(select(User).where(User.email == normalized))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user_roles(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    result = await db.execute(select(UserRole.role).where(UserRole.user_id == user_id))
    return [r.value if hasattr(r, "value") else str(r) for r in result.scalars().all()]


async def issue_session(
    db: AsyncSession,
    *,
    user: User,
    roles: list[str],
    parent_id: uuid.UUID | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[str, str, int, UserSession]:
    """Create a new session + tokens.

    Returns (access_token, refresh_token, access_expires_in, session).
    Only the refresh token hash is stored.
    """
    refresh_token = generate_opaque_token()
    refresh_hash = hash_token(refresh_token)
    expires_at = utcnow() + dt.timedelta(seconds=settings.REFRESH_TOKEN_TTL_SECONDS)

    session = UserSession(
        user_id=user.id,
        refresh_token_hash=refresh_hash,
        parent_id=parent_id,
        user_agent=user_agent,
        ip=ip,
        expires_at=expires_at,
    )
    db.add(session)
    await db.flush()

    access_token, _, _ = create_access_token(subject=str(user.id), roles=roles)
    return access_token, refresh_token, settings.ACCESS_TOKEN_TTL_SECONDS, session


async def find_session_by_refresh(db: AsyncSession, refresh_token: str) -> UserSession | None:
    token_hash = hash_token(refresh_token)
    result = await db.execute(
        select(UserSession).where(UserSession.refresh_token_hash == token_hash)
    )
    return result.scalar_one_or_none()


async def revoke_session(db: AsyncSession, session: UserSession) -> None:
    if session.revoked_at is None:
        session.revoked_at = utcnow()
        await db.flush()


async def revoke_chain(db: AsyncSession, session: UserSession) -> int:
    """Revoke the entire rotation chain reachable from ``session``.

    Walks parent_id links in both directions (ancestors + descendants) within
    the same user and revokes every session in the connected component.
    Returns the number of sessions newly revoked.
    """
    result = await db.execute(
        select(UserSession).where(UserSession.user_id == session.user_id)
    )
    all_sessions = list(result.scalars().all())
    by_id = {s.id: s for s in all_sessions}

    # Build the connected component via parent_id edges (undirected traversal).
    component: set[uuid.UUID] = set()
    stack = [session.id]
    while stack:
        current = stack.pop()
        if current in component:
            continue
        component.add(current)
        node = by_id.get(current)
        if node is not None and node.parent_id is not None and node.parent_id not in component:
            stack.append(node.parent_id)
        # children: any session whose parent is `current`
        for s in all_sessions:
            if s.parent_id == current and s.id not in component:
                stack.append(s.id)

    count = 0
    now = utcnow()
    for sid in component:
        s = by_id.get(sid)
        if s is not None and s.revoked_at is None:
            s.revoked_at = now
            count += 1
    await db.flush()
    return count


async def revoke_all_user_sessions(db: AsyncSession, user_id: uuid.UUID) -> int:
    result = await db.execute(select(UserSession).where(UserSession.user_id == user_id))
    count = 0
    for s in result.scalars().all():
        if s.revoked_at is None:
            s.revoked_at = utcnow()
            count += 1
    await db.flush()
    return count
