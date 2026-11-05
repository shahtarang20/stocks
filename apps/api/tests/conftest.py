"""Pytest fixtures: offline SQLite DB + httpx ASGI client.

Environment is configured BEFORE any app import so cached settings pick up the
test values (SQLite + ENVIRONMENT=test so Redis is never contacted).
"""

from __future__ import annotations

import os

# B3: set TEST_DATABASE_URL (e.g. postgresql+asyncpg://user:pw@localhost/test) to run the whole suite on
# Postgres. The default stays in-memory SQLite.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
IS_POSTGRES = TEST_DATABASE_URL.startswith(("postgresql", "postgres"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL or "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("AUTH_SECRET", "test-auth-secret")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("EMAIL_PROVIDER", "console")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool, StaticPool  # noqa: E402

from app.core.rate_limit import rate_limiter  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import configure_engine  # noqa: E402
import app.models  # noqa: E402,F401  (populate metadata)
from app.main import app  # noqa: E402


def pytest_collection_modifyitems(config, items):
    """Tests marked ``postgres_only`` are skipped on SQLite (they need real row locking)."""
    if IS_POSTGRES:
        return
    skip = pytest.mark.skip(reason="needs Postgres: set TEST_DATABASE_URL")
    for item in items:
        if "postgres_only" in item.keywords:
            item.add_marker(skip)


@pytest_asyncio.fixture
async def db_engine():
    if IS_POSTGRES:
        # A fresh schema per test; NullPool so concurrent sessions each get a real connection.
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    else:
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    configure_engine(engine)
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
