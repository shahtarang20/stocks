"""U15 ledger-level check: a Kit job run hold -> completion -> settle_job is never charged past its caps.

WRITTEN, NOT YET RUN. Uses the existing Phase 4/5 fixtures (simulated executor via ``post_result``), the real
``settle_job`` and the real ``record_actual_for_job`` completion hook. Kit fields are attached to an ordinary job
row right after submit, so nothing in billing, the ledger or pricing is changed or bypassed.
"""

from __future__ import annotations

import decimal
import uuid

from httpx import AsyncClient
from sqlalchemy import func, select

from app.core.config import settings
from app.db.session import get_sessionmaker
from app.models.billing import Invoice, LedgerEntry
from app.models.jobs import Job
from app.models.kits import Kit
from app.models.unit_quotes import UnitQuote, UnitQuoteSample
from app.services import ledger
from app.services import unit_quotes as uq
from tests.helpers import auth_headers, grant_role, register_verify_login
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import (
    _artifact_tmp,  # noqa: F401  (autouse fixture: object store + size cap)
    claim,
    post_result,
    seed_echo_model,
    submit_job,
)

D = decimal.Decimal
SLUG, VERSION, UNIT = "unit-quote-kit", "1.0.0", "frame"
MANIFEST = {"billing_hints": {"unit": UNIT, "est_gpu_seconds_per_unit": 30}}
MAX_COST = D("10.00")


async def _customer(client: AsyncClient, email: str) -> str:
    tok = (await register_verify_login(client, email))["access_token"]
    return tok


async def _ensure_kit() -> Kit:
    async with get_sessionmaker()() as db:
        kit = (await db.execute(select(Kit).where(Kit.slug == SLUG, Kit.version == VERSION))).scalar_one_or_none()
        if kit is None:
            kit = Kit(slug=SLUG, version=VERSION, name="Quote kit", category="render", image_digest="x@sha256:" + "0" * 64,
                      manifest=MANIFEST, manifest_sha256="0" * 64, signature="0" * 128, signing_key_id="test",
                      status="published")
            db.add(kit)
            await db.commit()
        return kit


async def _run_kit_job(client: AsyncClient, cust: str, machine: dict, *, units: int, seconds: int = 600) -> uuid.UUID:
    """Submit (hold), turn the job into a Kit job with a linked unit quote, run it to completion (settle)."""
    sub = await submit_job(client, cust, estimated_minutes=60, max_cost=str(MAX_COST))
    assert sub.status_code == 201, sub.text
    job_id = uuid.UUID(sub.json()["job_id"])
    kit = await _ensure_kit()
    async with get_sessionmaker()() as db:
        job = await db.get(Job, job_id)
        job.kit_id, job.kit_version = SLUG, VERSION
        job.spec_v2 = {"spec": {"billing": {"expected_units": units}}}
        quote = await uq.create_unit_quote(db, customer_id=job.customer_id, kit=kit, expected_units=units,
                                           gpu_count=1, max_cost=MAX_COST, price_per_hour=D("3.60"))
        await uq.link_quote_to_job(db, quote_id=quote.id, customer_id=job.customer_id, job=job)
        await db.commit()
    await claim(client, machine["machine_id"], machine["secret"])
    res = await post_result(
        client, machine["machine_id"], machine["secret"], str(job_id),
        {"status": "success", "execution_mode": "simulated", "output": {"text": "ok"},
         "metrics": {"duration_seconds": seconds}},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "completed"
    return job_id


async def test_kit_job_settles_within_caps_balanced_and_per_gpu_second(client, monkeypatch):
    monkeypatch.setattr(settings, "PRICING_UNITS_ENABLED", True)
    await seed_echo_model()
    m = await create_listable_machine(client, "p-uqs@x.com", hostname="uqs")
    cust = await _customer(client, "c-uqs@x.com")

    # Two jobs, same metered runtime, wildly different quoted units.
    small = await _run_kit_job(client, cust, m, units=10)
    large = await _run_kit_job(client, cust, m, units=1000)

    async with get_sessionmaker()() as db:
        for jid in (small, large):
            job = await db.get(Job, jid)
            quote = (await db.execute(select(UnitQuote).where(UnitQuote.job_id == jid))).scalar_one()
            invoice = (await db.execute(select(Invoice).where(Invoice.job_id == jid))).scalar_one()
            # charged <= job max_cost and <= the linked quote's cap
            assert job.final_cost is not None and D(job.final_cost) <= D(job.max_cost) == MAX_COST
            assert D(job.final_cost) <= D(quote.max_cost)
            assert invoice.actual_total_minor == ledger.to_minor(D(job.final_cost))
            assert invoice.actual_total_minor <= ledger.to_minor(MAX_COST)

        # Billing is metered GPU-seconds: the same runtime costs the same whatever units were quoted.
        j_small, j_large = await db.get(Job, small), await db.get(Job, large)
        assert D(j_small.final_cost) == D(j_large.final_cost) > 0

        # Ledger: every transaction balanced, and the grand totals match.
        txn_ids = (await db.execute(select(LedgerEntry.transaction_id).distinct())).scalars().all()
        for tid in txn_ids:
            sums = dict((await db.execute(
                select(LedgerEntry.direction, func.sum(LedgerEntry.amount_minor))
                .where(LedgerEntry.transaction_id == tid).group_by(LedgerEntry.direction))).all())
            assert sums.get("debit", 0) == sums.get("credit", 0)
        totals = dict((await db.execute(
            select(LedgerEntry.direction, func.sum(LedgerEntry.amount_minor)).group_by(LedgerEntry.direction))).all())
        assert totals["debit"] == totals["credit"]

        # record_actual_for_job (completion hook) stored one learning sample per job, with actual = GPU-seconds.
        samples = (await db.execute(select(UnitQuoteSample).where(UnitQuoteSample.kit_slug == SLUG))).scalars().all()
        assert sorted(s.units for s in samples) == [10, 1000]
        assert all(D(s.actual_gpu_seconds) == D(600) for s in samples)
        # Direct call is also well-behaved (returns a sample, never touches the ledger).
        before = totals
        again = await uq.record_actual_for_job(db, j_small)
        assert again is not None and again.units == 10
        after = dict((await db.execute(
            select(LedgerEntry.direction, func.sum(LedgerEntry.amount_minor)).group_by(LedgerEntry.direction))).all())
        assert after == before
