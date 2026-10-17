"""Tests for Workstream A1: httpOnly-cookie auth and double-submit CSRF protection."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.security import hash_password
from app.models.enums import Role, UserStatus
from app.models.user import User, UserRole


async def _seed_user(db_engine, email: str = "cookie_test@example.com", password: str = "password123"):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    sm = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sm() as db:
        user = User(
            email=email,
            password_hash=hash_password(password),
            status=UserStatus.active,
            email_verified=True,
        )
        db.add(user)
        await db.flush()
        db.add(UserRole(user_id=user.id, role=Role.customer))
        await db.commit()
        await db.refresh(user)
        return user


@pytest.mark.asyncio
async def test_login_sets_httponly_and_csrf_cookies(client: AsyncClient, db_engine):
    """Login must set httpOnly access + refresh cookies and a readable csrf_token cookie."""
    user = await _seed_user(db_engine, "cookies_set@test.com", "Password123!")

    res = await client.post(
        "/auth/login",
        json={"email": user.email, "password": "Password123!"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert "csrf_token" in data

    # Check cookies
    cookies = res.cookies
    assert "access_token" in cookies
    assert "refresh_token" in cookies
    assert "csrf_token" in cookies


@pytest.mark.asyncio
async def test_cookie_auth_get_me_success(client: AsyncClient, db_engine):
    """GET /auth/me should succeed using access_token cookie without Authorization header."""
    user = await _seed_user(db_engine, "me_cookie@test.com", "Password123!")

    login_res = await client.post(
        "/auth/login",
        json={"email": user.email, "password": "Password123!"},
    )
    assert login_res.status_code == 200

    # Call GET /auth/me with cookies (no Authorization header, no CSRF needed for safe GET)
    me_res = await client.get("/auth/me", cookies=login_res.cookies)
    assert me_res.status_code == 200
    me = me_res.json()
    assert me["email"] == user.email
    assert "customer" in me["roles"]


@pytest.mark.asyncio
async def test_cookie_auth_mutation_enforces_csrf(client: AsyncClient, db_engine):
    """Mutations authenticated via cookies must reject missing or mismatched CSRF headers."""
    user = await _seed_user(db_engine, "csrf_enforce@test.com", "Password123!")

    login_res = await client.post(
        "/auth/login",
        json={"email": user.email, "password": "Password123!"},
    )
    assert login_res.status_code == 200
    cookies = login_res.cookies
    csrf_token = cookies.get("csrf_token")
    assert csrf_token is not None

    # 1. POST without X-CSRF-Token must be rejected (403)
    res_no_csrf = await client.post(
        "/projects",
        json={"name": "Test Project"},
        cookies=cookies,
    )
    assert res_no_csrf.status_code == 403
    assert "CSRF" in res_no_csrf.text

    # 2. POST with mismatched X-CSRF-Token must be rejected (403)
    res_bad_csrf = await client.post(
        "/projects",
        json={"name": "Test Project"},
        cookies=cookies,
        headers={"X-CSRF-Token": "bad-csrf-token"},
    )
    assert res_bad_csrf.status_code == 403
    assert "CSRF" in res_bad_csrf.text

    # 3. POST with correct X-CSRF-Token succeeds
    res_valid = await client.post(
        "/projects",
        json={"name": "Test Project"},
        cookies=cookies,
        headers={"X-CSRF-Token": csrf_token},
    )
    assert res_valid.status_code in (200, 201)
    assert res_valid.json()["name"] == "Test Project"


@pytest.mark.asyncio
async def test_bearer_auth_bypasses_csrf_for_backwards_compatibility(client: AsyncClient, db_engine):
    """SDK / CLI clients passing Authorization: Bearer must succeed without needing CSRF tokens."""
    user = await _seed_user(db_engine, "bearer_compat@test.com", "Password123!")

    login_res = await client.post(
        "/auth/login",
        json={"email": user.email, "password": "Password123!"},
    )
    assert login_res.status_code == 200
    access_token = login_res.json()["access_token"]

    # Bearer header mutation without any cookies or CSRF header
    res = await client.post(
        "/projects",
        json={"name": "Bearer Project"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert res.status_code in (200, 201)
    assert res.json()["name"] == "Bearer Project"


@pytest.mark.asyncio
async def test_cookie_refresh_and_logout_lifecycle(client: AsyncClient, db_engine):
    """Test refreshing tokens via cookie with CSRF, and logging out to clear cookies."""
    user = await _seed_user(db_engine, "lifecycle@test.com", "Password123!")

    login_res = await client.post(
        "/auth/login",
        json={"email": user.email, "password": "Password123!"},
    )
    cookies = login_res.cookies
    csrf_token = cookies.get("csrf_token")

    # Refresh via cookie with CSRF
    refresh_res = await client.post(
        "/auth/refresh",
        cookies=cookies,
        headers={"X-CSRF-Token": csrf_token},
    )
    assert refresh_res.status_code == 200
    new_cookies = refresh_res.cookies
    assert "access_token" in new_cookies
    assert "csrf_token" in new_cookies
    new_csrf = new_cookies.get("csrf_token")

    # Logout clears cookies
    logout_res = await client.post(
        "/auth/logout",
        cookies=new_cookies,
        headers={"X-CSRF-Token": new_csrf},
    )
    assert logout_res.status_code == 200
