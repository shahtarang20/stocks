"""Race-condition tests that REQUIRE real PostgreSQL.

Why a separate suite: the default test DB is in-memory SQLite behind a
StaticPool. SQLAlchemy's SQLite dialect silently DROPS ``FOR UPDATE``, and the
pool has a single writer, so a test asserting row-level locking there passes
identically with and without the lock. Two fixes in this codebase are exactly
that shape and were shipped unproven:

* ``webhooks.deliver_due`` claims rows with ``FOR UPDATE SKIP LOCKED`` so two
  API workers' sweepers cannot both POST the same signed event. The
  ``delivery_key`` UNIQUE constraint makes ENQUEUE idempotent; it does nothing
  for DELIVERY.
* ``reputation.recompute_reputation`` inserts the first-ever row inside a
  SAVEPOINT, because two concurrent first-time recomputes both INSERT against a
  UNIQUE ``provider_id`` and the loser's IntegrityError would otherwise poison
  the whole transaction (and be swallowed by ``safe_hook``, silently dropping a
  provider penalty).

Both tests below run two genuinely concurrent sessions on separate connections
and FAIL when the corresponding fix is reverted.

Run:
    # start a throwaway postgres, then
    RUN_PG_INTEGRATION=1 \
    PG_INTEGRATION_DSN=postgresql+asyncpg://ccdev@127.0.0.1:5546/computecoin \
    .venv/bin/pytest -q tests/test_pg_concurrency_live.py

Opted in but unreachable is a FAILURE, never a skip: a CI job that quietly
skips its only concurrency coverage is how an unproven fix looks green forever.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base, utcnow

_OPT_IN = "RUN_PG_INTEGRATION"
_DEFAULT_DSN = "postgresql+asyncpg://ccdev@127.0.0.1:5546/computecoin"
_SKIP_REASON = (
    f"{_OPT_IN} is not set. These tests need real PostgreSQL (SQLite drops "
    "FOR UPDATE, so the lock cannot be observed). Start one and run:\n"
    f"  {_OPT_IN}=1 PG_INTEGRATION_DSN={_DEFAULT_DSN} pytest -q "
    "tests/test_pg_concurrency_live.py"
)


def _opted_in() -> bool:
    return os.environ.get(_OPT_IN, "").strip() not in ("", "0", "false", "False")


def _dsn() -> str:
    return os.environ.get("PG_INTEGRATION_DSN", _DEFAULT_DSN)


@pytest.fixture
async def pg_sessions():
    """Two sessionmakers on INDEPENDENT connections against real Postgres."""
    dsn = _dsn()
    if not dsn.startswith("postgresql"):
        pytest.fail(f"PG_INTEGRATION_DSN must be a postgresql DSN, got {dsn!r}")

    engine = create_async_engine(dsn, future=True, pool_size=8, max_overflow=8)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        # Opted in: an unreachable DB must FAIL, not skip.
        pytest.fail(f"Could not reach PostgreSQL at {dsn}: {exc}")

    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sm
    finally:
        await engine.dispose()


async def _mk_user(sm, email: str):
    from app.core.security import hash_password
    from app.models.enums import UserStatus
    from app.models.user import User

    async with sm() as db:
        user = User(
            id=uuid.uuid4(),
            email=email,
            password_hash=hash_password("Password123!"),
            email_verified=True,
            status=UserStatus.active,
        )
        db.add(user)
        await db.commit()
        return user.id


# ---------------------------------------------------------------------------
# webhooks.deliver_due — two sweepers must not both deliver one event
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _opted_in(), reason=_SKIP_REASON)
@pytest.mark.asyncio
async def test_two_sweepers_never_deliver_the_same_event_twice(pg_sessions):
    from app.models.webhooks import WebhookDelivery, WebhookEndpoint
    from app.services.webhooks import deliver_due

    sm = pg_sessions
    owner_id = await _mk_user(sm, f"hook-{uuid.uuid4().hex[:8]}@pg.test")

    endpoint_id = uuid.uuid4()
    async with sm() as db:
        db.add(
            WebhookEndpoint(
                id=endpoint_id,
                customer_id=owner_id,
                url="https://example.com/hook",
                secret_hash="x" * 64,
                events=["job.completed"],
                active=True,
            )
        )
        await db.flush()
        past = utcnow() - dt.timedelta(minutes=5)
        for i in range(12):
            db.add(
                WebhookDelivery(
                    id=uuid.uuid4(),
                    endpoint_id=endpoint_id,
                    event_type="job.completed",
                    payload={"n": i},
                    delivery_key=f"k-{uuid.uuid4().hex}",
                    status="pending",
                    attempts=0,
                    next_attempt_at=past,
                )
            )
        await db.commit()

    async def public_resolver(host: str, port: int) -> list[str]:
        """Keep the test offline: the SSRF guard otherwise does real DNS."""
        return ["93.184.216.34"]

    seen: dict[str, list[str]] = {"a": [], "b": []}
    gate = asyncio.Event()

    def make_sender(who: str):
        async def sender(url, headers, body):
            from app.services.webhooks import SendResult

            seen[who].append(headers["X-ComputeCoin-Delivery"])
            # Hold the transaction open so both sweepers genuinely overlap.
            await asyncio.sleep(0.25)
            return SendResult(status_code=200, error=None)

        return sender

    async def sweep(who: str):
        async with sm() as db:
            await gate.wait()
            await deliver_due(
                db, sender=make_sender(who), resolver=public_resolver, limit=100
            )
            await db.commit()

    task_a = asyncio.create_task(sweep("a"))
    task_b = asyncio.create_task(sweep("b"))
    await asyncio.sleep(0.2)
    gate.set()
    await asyncio.gather(task_a, task_b)

    overlap = set(seen["a"]) & set(seen["b"])
    assert not overlap, (
        f"{len(overlap)} delivery/ies were POSTed by BOTH sweepers: {sorted(overlap)[:3]}. "
        "FOR UPDATE SKIP LOCKED is not claiming rows, so every API worker "
        "re-sends the same signed event to the customer."
    )
    assert len(seen["a"]) + len(seen["b"]) == 12, (
        f"expected all 12 deliveries attempted exactly once across both "
        f"sweepers, got {len(seen['a'])} + {len(seen['b'])}"
    )

    async with sm() as db:
        from sqlalchemy import func, select

        remaining = await db.scalar(
            select(func.count())
            .select_from(WebhookDelivery)
            .where(
                WebhookDelivery.endpoint_id == endpoint_id,
                WebhookDelivery.status == "pending",
            )
        )
    assert remaining == 0, f"{remaining} deliveries left pending after both sweeps"


# ---------------------------------------------------------------------------
# reputation.recompute_reputation — concurrent first-time inserts
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _opted_in(), reason=_SKIP_REASON)
@pytest.mark.asyncio
async def test_concurrent_first_recompute_does_not_poison_the_transaction(pg_sessions):
    """Two first-time recomputes race on a UNIQUE provider_id.

    Without the SAVEPOINT the loser's IntegrityError aborts the whole
    transaction, so the fallback SELECT fails with "current transaction is
    aborted" and the outcome is lost.
    """
    from sqlalchemy import func, select

    from app.models.provider import Provider
    from app.models.reputation import ProviderReputation
    from app.services.reputation import recompute_reputation

    sm = pg_sessions
    user_id = await _mk_user(sm, f"rep-{uuid.uuid4().hex[:8]}@pg.test")
    provider_id = uuid.uuid4()
    async with sm() as db:
        db.add(
            Provider(
                id=provider_id,
                user_id=user_id,
                display_name="PG Race Fleet",
                contact_email="rep@pg.test",
                status="active",
            )
        )
        await db.commit()

    gate = asyncio.Event()
    errors: list[BaseException] = []

    async def worker():
        try:
            async with sm() as db:
                await gate.wait()
                await recompute_reputation(db, provider_id, event_kind="decay")
                await db.commit()
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
            errors.append(exc)

    tasks = [asyncio.create_task(worker()) for _ in range(2)]
    await asyncio.sleep(0.15)
    gate.set()
    await asyncio.gather(*tasks)

    assert not errors, (
        "a concurrent first-time recompute raised "
        f"{type(errors[0]).__name__}: {errors[0]}. Without the SAVEPOINT the "
        "loser's IntegrityError aborts the transaction and the provider's "
        "penalty is silently dropped."
    )

    async with sm() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(ProviderReputation)
            .where(ProviderReputation.provider_id == provider_id)
        )
    assert count == 1, f"expected exactly one reputation row, found {count}"
