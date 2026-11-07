"""B3: concurrency tests that only mean something on real Postgres row/advisory locking.

Run with TEST_DATABASE_URL pointing at Postgres; skipped on SQLite (marker ``postgres_only``).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.privacy import TransparencyLogLeaf
from app.services.ledger import get_platform_account, ledger_totals, post_transaction
from app.services.transparency_log import append_transparency_log_entry

pytestmark = pytest.mark.postgres_only


async def test_concurrent_log_appends_get_distinct_indices(db_engine):
    sm = get_sessionmaker()

    async def append(i: int) -> None:
        async with sm() as db:
            await append_transparency_log_entry(db, "IMAGE_HASH", {"n": i})
            await db.commit()

    await asyncio.gather(*(append(i) for i in range(12)))
    async with sm() as db:
        indices = list((await db.execute(select(TransparencyLogLeaf.leaf_index))).scalars().all())
    assert sorted(indices) == list(range(12))


async def test_concurrent_ledger_postings_stay_balanced(db_engine):
    sm = get_sessionmaker()

    async def post(i: int) -> None:
        async with sm() as db:
            external = await get_platform_account(db, "external")
            holding = await get_platform_account(db, "holding")
            await post_transaction(
                db,
                kind="test_concurrent",
                legs=[(external, "debit", 100 + i), (holding, "credit", 100 + i)],
                idempotency_key=f"b3-{uuid.uuid4()}",
            )
            await db.commit()

    # Create the singleton accounts first so the race is on postings, not on account creation.
    async with sm() as db:
        await get_platform_account(db, "external")
        await get_platform_account(db, "holding")
        await db.commit()

    await asyncio.gather(*(post(i) for i in range(10)))
    async with sm() as db:
        debits, credits = await ledger_totals(db)
    assert debits == credits == sum(100 + i for i in range(10))


async def test_idempotent_posting_under_concurrency_applies_once(db_engine):
    sm = get_sessionmaker()
    async with sm() as db:
        await get_platform_account(db, "external")
        await get_platform_account(db, "holding")
        await db.commit()

    key = f"b3-same-{uuid.uuid4()}"

    async def post() -> None:
        async with sm() as db:
            external = await get_platform_account(db, "external")
            holding = await get_platform_account(db, "holding")
            await post_transaction(
                db,
                kind="test_concurrent",
                legs=[(external, "debit", 500), (holding, "credit", 500)],
                idempotency_key=key,
            )
            await db.commit()

    await asyncio.gather(*(post() for _ in range(5)), return_exceptions=True)
    async with sm() as db:
        debits, credits = await ledger_totals(db)
    assert debits == credits == 500


# --- B3 additions: claims, lease recovery, key release (WRITTEN, NOT YET RUN; need Postgres) -------------

import datetime as dt  # noqa: E402

from app.models.jobs import Job  # noqa: E402
from app.models.provider import ProviderMachine  # noqa: E402
from app.services.leases import recover_expired_leases  # noqa: E402


async def test_two_concurrent_claims_yield_one_job(client, db_engine):
    from tests.test_phase3_marketplace import create_listable_machine
    from tests.test_phase4_jobs import _customer, claim, seed_echo_model, submit_job

    await seed_echo_model()
    m = await create_listable_machine(client, "pg-claim-p@x.com", hostname="pgc")
    cust = await _customer(client, "pg-claim-c@x.com")
    assert (await submit_job(client, cust)).status_code == 201
    results = await asyncio.gather(*(claim(client, m["machine_id"], m["secret"]) for _ in range(2)))
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 204], codes  # exactly one claim wins the RESERVED job
    async with get_sessionmaker()() as db:
        running = (await db.execute(select(Job).where(Job.status == "running"))).scalars().all()
    assert len(running) == 1


async def test_lease_recovery_with_skip_locked_never_double_recovers(client, db_engine):
    from tests.test_phase3_marketplace import create_listable_machine
    from tests.test_phase4_jobs import _customer, claim, seed_echo_model, submit_job

    await seed_echo_model()
    m = await create_listable_machine(client, "pg-lease-p@x.com", hostname="pgl")
    cust = await _customer(client, "pg-lease-c@x.com")
    assert (await submit_job(client, cust)).status_code == 201  # max_retries 0: recovery times it out
    assert (await claim(client, m["machine_id"], m["secret"])).status_code == 200
    sm = get_sessionmaker()
    async with sm() as db:
        job = (await db.execute(select(Job).where(Job.status == "running"))).scalar_one()
        job.lease_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        await db.commit()

    async def recover() -> list[dict]:
        async with sm() as db:
            out = await recover_expired_leases(db)
            await asyncio.sleep(0.2)  # hold the row lock a moment so the other sweeper really overlaps
            await db.commit()
            return out

    a, b = await asyncio.gather(recover(), recover())
    assert len(a) + len(b) == 1, (a, b)  # recovered once in total
    async with sm() as db:
        job = (await db.execute(select(Job))).scalar_one()
    assert job.status == "timed_out" and job.attempts_made == 1


async def test_concurrent_key_release_requests_release_once(db_engine, monkeypatch):
    from app.core.config import settings as s
    from app.models.enums import Role
    from app.services.key_guardians import GuardianReleaseError, release_guardian_shares
    from app.models.privacy import TransparencyLogLeaf as Leaf
    from tests import test_proof_of_privacy as pp

    monkeypatch.setattr(s, "PRIVACY_SOFTWARE_ATTESTATION", True)
    host, _ = await pp._user(db_engine, "pg-h@test.io", Role.provider)
    cust, _ = await pp._user(db_engine, "pg-c@test.io")
    m = await pp._machine(db_engine, host, "pgk", tiers=["container"], stake="2000", signing_key="k-pg")
    await pp._trusted_image(db_engine)
    job, _, _ = await pp._assigned_private_job(db_engine, m, cust.id)
    verdict, _, _, _ = await pp._job_verdict(db_engine, m.id, job.id)
    sm = get_sessionmaker()

    async def release() -> str:
        from app.services.key_guardians import execute_guardian_release, prepare_guardian_release

        async with sm() as db:
            # Exactly the /v1/agents/jobs/{id}/keys route: lock the job row, check, mark "releasing", COMMIT (which frees the
            # lock) and only then release. A second concurrent request waits on the lock and then sees the marker.
            j = (await db.execute(select(Job).where(Job.id == job.id).with_for_update())).scalar_one()
            machine = await db.get(ProviderMachine, m.id)
            try:
                plan = await prepare_guardian_release(db, job=j, machine=machine, attestation_verdict=verdict)
                j.guardian_releases = [{"status": "releasing", "verdict_id": verdict["verdict_id"]}]
                await db.commit()
                got = await execute_guardian_release(db, job=j, machine=machine, attestation_verdict=verdict, plan=plan)
            except GuardianReleaseError as exc:
                await db.rollback()
                return exc.code if hasattr(exc, "code") else str(exc)
            return f"released:{len(got)}"

    outcomes = await asyncio.gather(release(), release())
    assert sum(o.startswith("released:") for o in outcomes) == 1, outcomes
    async with sm() as db:
        logs = (await db.execute(select(Leaf).where(Leaf.event_type == "KEY_RELEASE"))).scalars().all()
    assert len(logs) == 2  # one release = k log entries, not 2k
