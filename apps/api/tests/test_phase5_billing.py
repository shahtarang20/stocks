"""Phase 5 — billing, double-entry ledger, invoices, refunds, payouts, disputes.

Proves the money-layer correctness requirements:
 1. post_transaction balances; unbalanced legs refused (nothing written).
 2. idempotency: same key -> one txn, one effect.
 3. no negative balance: a hold beyond funds -> InsufficientFunds, balance never < 0.
 4. job happy path: hold -> capture + release remainder; invoice + usage written.
 5. no double charge: settling twice charges once.
 6. failed/cancelled job -> hold fully released, zero net charge.
 7. refund reverses a charge; reconciliation imbalance == 0.
 8. provider payout within earnings ok; beyond earnings 409; admin authorize settles.
 9. reconciliation (INVARIANT 2): SUM(debits) == SUM(credits), balanced + "0.00".
10. isolation: B cannot read A's invoice/dispute (404); non-admin -> 403 on admin.
11. money precision: integer minor units / Decimal, no float drift.
"""

from __future__ import annotations

import decimal
import uuid

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.db.session import get_sessionmaker
from app.models.billing import CURRENCY, BillingAccount, Invoice
from app.models.enums import Role
from app.models.jobs import Job
from app.services import billing as billing_svc
from app.services import ledger
from tests.helpers import auth_headers, get_user_id, grant_role, register_verify_login
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import (
    _artifact_tmp,  # noqa: F401  (autouse fixture: object store + size cap)
    claim,
    post_result,
    seed_echo_model,
    submit_job,
)

pytestmark = pytest.mark.asyncio


async def _customer(client: AsyncClient, email: str) -> str:
    tokens = await register_verify_login(client, email)
    return tokens["access_token"]


async def _admin(client: AsyncClient, email: str) -> str:
    tokens = await register_verify_login(client, email)
    await grant_role(email, Role.administrator)
    return tokens["access_token"]


async def _mk_account(db, type_, owner_type, owner_id=None) -> BillingAccount:
    acct = BillingAccount(owner_type=owner_type, owner_id=owner_id, type=type_, currency=CURRENCY)
    db.add(acct)
    await db.flush()
    return acct


# ===========================================================================
# 1. ledger balance / unbalanced refused
# ===========================================================================
async def test_balanced_posts_and_unbalanced_refused(db_engine):
    sm = get_sessionmaker()
    async with sm() as db:
        external = await ledger.get_platform_account(db, "external")
        cust = await _mk_account(db, "customer_credits", "customer", uuid.uuid4())

        txn = await ledger.post_transaction(
            db, kind="credit_purchase",
            legs=[(external, "debit", 10000), (cust, "credit", 10000)],
            idempotency_key="t1",
        )
        assert txn is not None
        await db.refresh(cust)
        assert cust.balance_minor == 10000

        # Unbalanced legs are refused and nothing is written.
        with pytest.raises(ledger.LedgerImbalance):
            await ledger.post_transaction(
                db, kind="adjustment",
                legs=[(external, "debit", 100), (cust, "credit", 99)],
                idempotency_key="t2-unbalanced",
            )
        await db.commit()

    async with sm() as db:
        from sqlalchemy import func, select
        from app.models.billing import LedgerTransaction
        count = (
            await db.execute(
                select(func.count()).select_from(LedgerTransaction).where(
                    LedgerTransaction.idempotency_key == "t2-unbalanced"
                )
            )
        ).scalar_one()
        assert count == 0  # the refused txn left no trace


# ===========================================================================
# 2. idempotency: one txn, one effect
# ===========================================================================
async def test_idempotency_one_effect(db_engine):
    sm = get_sessionmaker()
    async with sm() as db:
        external = await ledger.get_platform_account(db, "external")
        cust = await _mk_account(db, "customer_credits", "customer", uuid.uuid4())
        t1 = await ledger.post_transaction(
            db, kind="credit_purchase",
            legs=[(external, "debit", 5000), (cust, "credit", 5000)],
            idempotency_key="idem-key",
        )
        t2 = await ledger.post_transaction(
            db, kind="credit_purchase",
            legs=[(external, "debit", 5000), (cust, "credit", 5000)],
            idempotency_key="idem-key",
        )
        assert t1.id == t2.id  # same transaction returned
        await db.refresh(cust)
        assert cust.balance_minor == 5000  # charged once, not twice


# ===========================================================================
# 3. no negative balance under no funds
# ===========================================================================
async def test_no_negative_balance(db_engine):
    sm = get_sessionmaker()
    async with sm() as db:
        external = await ledger.get_platform_account(db, "external")
        holding = await ledger.get_platform_account(db, "holding")
        cust = await _mk_account(db, "customer_credits", "customer", uuid.uuid4())

        # Fund for exactly one job.
        await ledger.post_transaction(
            db, kind="credit_purchase",
            legs=[(external, "debit", 5000), (cust, "credit", 5000)],
            idempotency_key="fund",
        )
        # First hold uses the whole balance.
        await ledger.post_transaction(
            db, kind="hold",
            legs=[(cust, "debit", 5000), (holding, "credit", 5000)],
            idempotency_key="hold-1",
        )
        await db.refresh(cust)
        assert cust.balance_minor == 0

        # Second hold beyond balance is refused; balance never goes negative.
        with pytest.raises(ledger.InsufficientFunds):
            await ledger.post_transaction(
                db, kind="hold",
                legs=[(cust, "debit", 1), (holding, "credit", 1)],
                idempotency_key="hold-2",
            )
        await db.refresh(cust)
        assert cust.balance_minor == 0


# ===========================================================================
# helpers for the end-to-end money flow
# ===========================================================================
async def _run_job_to_completion(
    client: AsyncClient, cust_token: str, machine: dict, *, minutes: int = 120, seconds: int = 3600
) -> str:
    sub = await submit_job(client, cust_token, estimated_minutes=minutes)
    assert sub.status_code == 201, sub.text
    assert sub.json()["status"] == "reserved"
    job_id = sub.json()["job_id"]
    await claim(client, machine["machine_id"], machine["secret"])
    result = await post_result(
        client, machine["machine_id"], machine["secret"], job_id,
        {"status": "success", "execution_mode": "simulated",
         "output": {"text": "ok"}, "metrics": {"duration_seconds": seconds}},
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed"
    return job_id


# ===========================================================================
# 4. job happy path: hold -> capture + release remainder
# ===========================================================================
async def test_job_happy_path_capture_and_release(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-hp@x.com", hostname="hp")
    cust = await _customer(client, "c-hp@x.com")

    # On submit, the hold reduces available by the quote total (3.30 for 120 min).
    sub = await submit_job(client, cust, estimated_minutes=120)
    job_id = sub.json()["job_id"]
    acct = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    assert acct["available"] == "996.70"
    assert acct["reserved"] == "3.30"
    assert acct["balance"] == "1000.00"

    # Run to completion (1 metered hour -> actual 1.65).
    await claim(client, m["machine_id"], m["secret"])
    result = await post_result(
        client, m["machine_id"], m["secret"], job_id,
        {"status": "success", "execution_mode": "simulated",
         "output": {"text": "ok"}, "metrics": {"duration_seconds": 3600}},
    )
    assert result.json()["status"] == "completed"

    # Customer debited exactly actual_cost (1.65); holding released -> reserved 0.
    acct2 = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    assert acct2["available"] == "998.35"
    assert acct2["reserved"] == "0.00"
    assert acct2["balance"] == "998.35"

    # Invoice: estimated vs actual stored separately; platform fee split out.
    invoices = (await client.get("/v1/invoices", headers=auth_headers(cust))).json()
    assert len(invoices) == 1
    inv = invoices[0]
    assert inv["estimated_total"] == "3.30"
    assert inv["actual_total"] == "1.65"
    assert inv["platform_fee"] == "0.15"
    labels = {li["label"]: li["amount"] for li in inv["line_items"]}
    assert labels["compute"] == "1.50"
    assert labels["platform_fee"] == "0.15"

    # Usage record (estimated vs actual).
    usage = (await client.get("/v1/usage", headers=auth_headers(cust))).json()
    assert usage["total_actual"] == "1.65"
    assert usage["records"][0]["estimated_cost"] == "3.30"
    assert usage["records"][0]["actual_cost"] == "1.65"

    # Provider earnings credited the compute portion; platform fee is revenue.
    earnings = (await client.get("/v1/provider/earnings", headers=auth_headers(m["access"]))).json()
    assert earnings["available"] == "1.50"
    assert earnings["lifetime"] == "1.50"

    # Holding back to 0; whole ledger balanced.
    admin = await _admin(client, "admin-hp@x.com")
    recon = (await client.get("/v1/admin/reconciliation", headers=auth_headers(admin))).json()
    assert recon["balanced"] is True
    assert recon["imbalance"] == "0.00"
    holding = next(a for a in recon["accounts"] if a["type"] == "holding")
    assert holding["balance"] == "0.00"
    platform_rev = next(a for a in recon["accounts"] if a["type"] == "platform_revenue")
    assert platform_rev["balance"] == "0.15"


# ===========================================================================
# 5. no double charge
# ===========================================================================
async def test_no_double_charge(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-dc@x.com", hostname="dc")
    cust = await _customer(client, "c-dc@x.com")
    job_id = await _run_job_to_completion(client, cust, m)

    acct1 = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()

    # Settle the SAME job again directly -> no further effect (idempotent).
    sm = get_sessionmaker()
    async with sm() as db:
        job = await db.get(Job, uuid.UUID(job_id))
        await billing_svc.settle_job(db, job)
        await billing_svc.settle_job(db, job)
        await db.commit()

    acct2 = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    assert acct1 == acct2  # balances identical after repeated settle

    async with sm() as db:
        from sqlalchemy import func, select
        n = (
            await db.execute(
                select(func.count()).select_from(Invoice).where(
                    Invoice.job_id == uuid.UUID(job_id)
                )
            )
        ).scalar_one()
        assert n == 1  # exactly one invoice / one capture


# ===========================================================================
# 6. failed / cancelled -> hold fully released, zero net charge
# ===========================================================================
async def test_failed_job_not_charged(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-fl@x.com", hostname="fl")
    cust = await _customer(client, "c-fl@x.com")
    sub = await submit_job(client, cust, estimated_minutes=120)
    job_id = sub.json()["job_id"]
    await claim(client, m["machine_id"], m["secret"])
    result = await post_result(
        client, m["machine_id"], m["secret"], job_id,
        {"status": "failed", "execution_mode": "simulated",
         "error": {"code": "oom", "message": "out of memory"}},
    )
    assert result.json()["status"] == "failed"

    # Full hold returned; zero net charge; no revenue recognized.
    acct = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    assert acct["available"] == "1000.00"
    assert acct["reserved"] == "0.00"
    invoices = (await client.get("/v1/invoices", headers=auth_headers(cust))).json()
    assert invoices == []
    earnings = (await client.get("/v1/provider/earnings", headers=auth_headers(m["access"]))).json()
    assert earnings["available"] == "0.00"


async def test_cancelled_job_not_charged(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-cx@x.com", hostname="cx")
    cust = await _customer(client, "c-cx@x.com")
    sub = await submit_job(client, cust, estimated_minutes=120)
    job_id = sub.json()["job_id"]

    cancel = await client.post(f"/v1/jobs/{job_id}/cancel", json={}, headers=auth_headers(cust))
    assert cancel.json()["status"] == "cancelled"
    acct = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    assert acct["available"] == "1000.00"
    assert acct["reserved"] == "0.00"


# ===========================================================================
# 7. refund reverses a charge; reconciliation imbalance == 0
# ===========================================================================
async def test_refund_reverses_charge(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-rf5@x.com", hostname="rf5")
    cust = await _customer(client, "c-rf5@x.com")
    job_id = await _run_job_to_completion(client, cust, m)
    admin = await _admin(client, "admin-rf@x.com")

    before = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    assert before["available"] == "998.35"

    refund = await client.post(
        "/v1/admin/refunds",
        json={"job_id": job_id, "amount": "1.00", "reason": "goodwill"},
        headers=auth_headers(admin),
    )
    assert refund.status_code == 201, refund.text
    assert refund.json()["amount"] == "1.00"

    after = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    assert after["available"] == "999.35"  # 998.35 + 1.00 refunded

    recon = (await client.get("/v1/admin/reconciliation", headers=auth_headers(admin))).json()
    assert recon["balanced"] is True
    assert recon["imbalance"] == "0.00"


# ===========================================================================
# 7b. refunds are capped CUMULATIVELY against the captured amount (C2) —
#     repeated refunds cannot exceed what was charged (no money minting).
# ===========================================================================
async def test_refund_cumulative_cap(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-rfcap@x.com", hostname="rfcap")
    cust = await _customer(client, "c-rfcap@x.com")
    job_id = await _run_job_to_completion(client, cust, m)  # captured total 1.65
    admin = await _admin(client, "admin-rfcap@x.com")

    # First refund of 1.00 succeeds.
    r1 = await client.post(
        "/v1/admin/refunds",
        json={"job_id": job_id, "amount": "1.00", "reason": "partial"},
        headers=auth_headers(admin),
    )
    assert r1.status_code == 201, r1.text

    # Second refund of 1.00 would make cumulative 2.00 > 1.65 captured -> rejected.
    r2 = await client.post(
        "/v1/admin/refunds",
        json={"job_id": job_id, "amount": "1.00", "reason": "double"},
        headers=auth_headers(admin),
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["detail"] == "invalid_refund_amount"

    # A further refund up to the remaining 0.65 is allowed.
    r3 = await client.post(
        "/v1/admin/refunds",
        json={"job_id": job_id, "amount": "0.65", "reason": "remainder"},
        headers=auth_headers(admin),
    )
    assert r3.status_code == 201, r3.text

    # Ledger remains balanced after the capped refunds.
    recon = (await client.get("/v1/admin/reconciliation", headers=auth_headers(admin))).json()
    assert recon["balanced"] is True
    assert recon["imbalance"] == "0.00"


# ===========================================================================
# 8. provider payout: within ok, beyond 409, admin authorize settles
# ===========================================================================
async def test_provider_payout_flow(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-po@x.com", hostname="po")
    cust = await _customer(client, "c-po@x.com")
    await _run_job_to_completion(client, cust, m)  # provider earns 1.50
    prov = m["access"]

    # Beyond available -> 409 insufficient_earnings.
    over = await client.post("/v1/provider/payouts", json={"amount": "100.00"}, headers=auth_headers(prov))
    assert over.status_code == 409
    assert over.json()["detail"] == "insufficient_earnings"

    # Within available -> ok (requested).
    ok = await client.post("/v1/provider/payouts", json={"amount": "1.00"}, headers=auth_headers(prov))
    assert ok.status_code == 201, ok.text
    payout_id = ok.json()["payout_id"]
    assert ok.json()["status"] == "requested"

    # Earnings reduced by the pending payout (1.50 -> 0.50).
    earnings = (await client.get("/v1/provider/earnings", headers=auth_headers(prov))).json()
    assert earnings["available"] == "0.50"

    # Admin authorizes -> settled; ledger stays balanced.
    admin = await _admin(client, "admin-po@x.com")
    auth = await client.post(f"/v1/admin/payouts/{payout_id}/authorize", headers=auth_headers(admin))
    assert auth.status_code == 200, auth.text
    assert auth.json()["status"] == "settled"
    recon = (await client.get("/v1/admin/reconciliation", headers=auth_headers(admin))).json()
    assert recon["balanced"] is True


# ===========================================================================
# 9. reconciliation after a mix of purchases/holds/captures/releases/refunds
# ===========================================================================
async def test_reconciliation_after_mixed_activity(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-mx@x.com", hostname="mx", max_concurrent_jobs=4)
    cust = await _customer(client, "c-mx@x.com")
    admin = await _admin(client, "admin-mx@x.com")

    await client.post("/v1/billing/credits/purchase", json={"amount": "50.00"}, headers=auth_headers(cust))
    j1 = await _run_job_to_completion(client, cust, m)            # capture + release
    # A failed job (hold fully released).
    sub = await submit_job(client, cust, estimated_minutes=60)
    await claim(client, m["machine_id"], m["secret"])
    await post_result(
        client, m["machine_id"], m["secret"], sub.json()["job_id"],
        {"status": "failed", "execution_mode": "simulated", "error": {"code": "x", "message": "y"}},
    )
    # A refund on the completed job.
    await client.post(
        "/v1/admin/refunds", json={"job_id": j1, "amount": "0.50", "reason": "mix"},
        headers=auth_headers(admin),
    )

    recon = (await client.get("/v1/admin/reconciliation", headers=auth_headers(admin))).json()
    assert recon["balanced"] is True
    assert recon["imbalance"] == "0.00"
    assert recon["total_debits"] == recon["total_credits"]


# ===========================================================================
# 10. isolation + RBAC
# ===========================================================================
async def test_isolation_and_rbac(client):
    await seed_echo_model()
    m = await create_listable_machine(client, "p-iso@x.com", hostname="iso")
    cust_a = await _customer(client, "c-iso-a@x.com")
    cust_b = await _customer(client, "c-iso-b@x.com")
    job_id = await _run_job_to_completion(client, cust_a, m)

    inv_id = (await client.get("/v1/invoices", headers=auth_headers(cust_a))).json()[0]["invoice_id"]
    # B cannot read A's invoice.
    assert (await client.get(f"/v1/invoices/{inv_id}", headers=auth_headers(cust_b))).status_code == 404
    assert (await client.get(f"/v1/invoices/{inv_id}/download", headers=auth_headers(cust_b))).status_code == 404

    # A opens a dispute; B cannot read it.
    d = await client.post(
        f"/v1/jobs/{job_id}/dispute", json={"reason": "bad output"}, headers=auth_headers(cust_a)
    )
    assert d.status_code == 201, d.text
    dispute_id = d.json()["dispute_id"]
    assert (await client.get(f"/v1/disputes/{dispute_id}", headers=auth_headers(cust_b))).status_code == 404
    # B's dispute list is empty (own only).
    assert (await client.get("/v1/disputes", headers=auth_headers(cust_b))).json() == []

    # Non-admin cannot hit admin money endpoints (403).
    assert (await client.get("/v1/admin/reconciliation", headers=auth_headers(cust_b))).status_code == 403
    r = await client.post(
        "/v1/admin/refunds", json={"job_id": job_id, "amount": "1.00", "reason": "x"},
        headers=auth_headers(cust_b),
    )
    assert r.status_code == 403


# ===========================================================================
# 11. money precision: no float drift
# ===========================================================================
async def test_money_precision_no_float_drift(client):
    cust = await _customer(client, "c-prec@x.com")
    # Baseline: the signup grant.
    base = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    assert base["available"] == "1000.00"

    # 100 top-ups of 0.01 each: exactly +1.00 (float 0.01*100 would drift).
    for _ in range(100):
        await client.post(
            "/v1/billing/credits/purchase", json={"amount": "0.01"}, headers=auth_headers(cust)
        )
    acct = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()
    assert acct["available"] == "1001.00"

    # And the ledger math is exact at the minor-unit level.
    total = decimal.Decimal("0")
    for _ in range(100):
        total += ledger.from_minor(1)
    assert total == decimal.Decimal("1.00")
    assert ledger.to_minor(decimal.Decimal("0.01")) == 1


async def test_only_one_open_dispute_per_job(client):
    """Nothing used to stop a customer opening unlimited disputes on one job; each
    one created another evidence bundle and another item for an admin to triage."""
    await seed_echo_model()
    m = await create_listable_machine(client, "p-dup-d@x.com", hostname="dupd")
    cust = await _customer(client, "c-dup-d@x.com")
    job_id = await _run_job_to_completion(client, cust, m)

    first = await client.post(
        f"/v1/jobs/{job_id}/dispute", json={"reason": "bad output"}, headers=auth_headers(cust)
    )
    assert first.status_code == 201, first.text
    again = await client.post(
        f"/v1/jobs/{job_id}/dispute", json={"reason": "bad output again"}, headers=auth_headers(cust)
    )
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == "dispute_exists"

    listed = (await client.get("/v1/disputes", headers=auth_headers(cust))).json()
    assert len(listed) == 1


@pytest.mark.parametrize(
    "bad",
    ["NaN", "Infinity", "-Infinity", "1e30", "0.001", "1.999", "abc", "5,00", "0x10", "",
     "99999999999999999999999", "1000000.01", 0, -5, True, None, "0", "-0.01"],
)
async def test_money_amounts_that_used_to_500_or_round_silently_are_422(client, bad):
    """AmountIn parsed with Decimal(str(v)): NaN/Infinity/'1e30'/garbage/booleans
    crashed with a 500, '1.999' was silently rounded to 2.00, and there was no
    ceiling. They must all be clean 422s and must never touch the ledger."""
    cust = await _customer(client, f"c-amt-{abs(hash(str(bad))) % 10**8}@x.com")
    before = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()["balance"]
    r = await client.post("/v1/billing/credits/purchase", json={"amount": bad}, headers=auth_headers(cust))
    assert r.status_code == 422, (bad, r.status_code, r.text)
    after = (await client.get("/v1/billing/account", headers=auth_headers(cust))).json()["balance"]
    assert after == before


@pytest.mark.parametrize("ok,credited", [("10", "10.00"), ("10.5", "10.50"), (" 7.25 ", "7.25"), (3, "3.00"), ("1000000", "1000000.00")])
async def test_well_formed_money_amounts_are_accepted(client, ok, credited):
    cust = await _customer(client, f"c-ok-{abs(hash(str(ok))) % 10**8}@x.com")
    before = float((await client.get("/v1/billing/account", headers=auth_headers(cust))).json()["balance"])
    r = await client.post("/v1/billing/credits/purchase", json={"amount": ok}, headers=auth_headers(cust))
    assert r.status_code == 200, r.text
    assert round(float(r.json()["balance"]) - before, 2) == float(credited)
