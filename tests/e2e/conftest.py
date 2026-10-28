"""Pytest fixtures for E2E tests: in-memory SQLite, rate limiter reset, and httpx client."""

from __future__ import annotations

import os
import sys

# Ensure all sub-packages are importable
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for p in [
    os.path.join(ROOT_DIR, "apps", "api"),
    os.path.join(ROOT_DIR, "provider-agent"),
    os.path.join(ROOT_DIR, "sdk", "python"),
    ROOT_DIR,
]:
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("AUTH_SECRET", "test-auth-secret")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("EMAIL_PROVIDER", "console")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.rate_limit import rate_limiter
from app.db.base import Base
from app.db.session import configure_engine
import app.models  # noqa: F401
from app.main import app


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    configure_engine(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limiter.reset()
    yield
    rate_limiter.reset()


@pytest_asyncio.fixture
async def client(db_engine) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
