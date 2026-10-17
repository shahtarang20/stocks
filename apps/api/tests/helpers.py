"""Shared test helpers."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from httpx import AsyncClient

from app.db.session import get_sessionmaker
from app.models.enums import Role
from app.models.user import User, UserRole


async def register_user(client: AsyncClient, email: str, password: str = "Sup3rStrongPw!") -> dict:
    resp = await client.post("/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def verify_email(client: AsyncClient, token: str) -> None:
    resp = await client.post("/auth/verify-email", json={"token": token})
    assert resp.status_code == 200, resp.text


async def login(client: AsyncClient, email: str, password: str = "Sup3rStrongPw!") -> dict:
    resp = await client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def register_verify_login(
    client: AsyncClient, email: str, password: str = "Sup3rStrongPw!"
) -> dict:
    reg = await register_user(client, email, password)
    await verify_email(client, reg["email_verification_token"])
    return await login(client, email, password)


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def grant_role(user_email: str, role: Role) -> None:
    """Directly grant a role to a user (no admin endpoint in Phase 1)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(User).where(User.email == user_email.strip().lower())
        )
        user = result.scalar_one()
        existing = await session.execute(
            select(UserRole).where(UserRole.user_id == user.id, UserRole.role == role)
        )
        if existing.scalar_one_or_none() is None:
            session.add(UserRole(user_id=user.id, role=role))
        await session.commit()


async def get_user_id(user_email: str) -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(User).where(User.email == user_email.strip().lower())
        )
        return result.scalar_one().id
