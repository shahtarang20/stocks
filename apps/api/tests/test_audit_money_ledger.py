"""Regression tests from the money / ledger / chain bug audit.

Each test pins a defect that was found by reading the code and verified to fail before the fix.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.db.session import get_sessionmaker
from app.models.billing import CURRENCY, BillingAccount
from app.services import ledger

pytestmark = pytest.mark.asyncio


async def _acct(db, type_, owner_type, owner_id=None, balance=0) -> BillingAccount:
    a = BillingAccount(owner_type=owner_type, owner_id=owner_id, type=type_, currency=CURRENCY, balance_minor=balance)
    db.add(a)
    await db.flush()
    return a


async def test_post_transaction_does_not_overwrite_a_concurrent_committers_balance(db_engine):
    """The session never expires on commit, so an account loaded earlier in a request keeps its old balance in the
    identity map. The locked SELECT inside post_transaction must REFRESH it, otherwise the write-back clobbers a credit
    that another transaction committed in between (a lost update on Postgres)."""
    sm = get_sessionmaker()
    async with sm() as db:
        external = await ledger.get_platform_account(db, "external")
        cust = await _acct(db, "customer_credits", "customer", uuid.uuid4(), balance=100)
        await db.commit()
        cust_id = cust.id
        # The caller loaded the account (balance 100) ...
        assert cust.balance_minor == 100
        # ... and another transaction committed a +50 credit behind its back.
        await db.execute(text("UPDATE billing_accounts SET balance_minor = 150 WHERE id = :i"), {"i": cust_id.hex})
        await ledger.post_transaction(
            db,
            kind="credit_purchase",
            legs=[(external, "debit", 1000), (cust, "credit", 1000)],
            idempotency_key="stale-identity-map",
        )
        await db.commit()
    async with sm() as db:
        fresh = await db.get(BillingAccount, cust_id)
        assert fresh.balance_minor == 1150  # 150 (the concurrent credit) + 1000, not 100 + 1000


# ---------------------------------------------------------------------------
# billing: agent-supplied duration must never break settlement
# ---------------------------------------------------------------------------
def test_metered_seconds_rejects_unusable_durations():
    import decimal

    from app.services import billing as billing_svc

    for bad in (None, True, "abc", "NaN", "Infinity", "-Infinity", -5, "-1", {"a": 1}, [1]):
        assert billing_svc._metered_seconds(bad) is None, bad
    assert billing_svc._metered_seconds(0) == decimal.Decimal(0)
    assert billing_svc._metered_seconds("12.5") == decimal.Decimal("12.5")
    assert billing_svc._metered_seconds(10**30) == billing_svc._MAX_METERED_SECONDS


async def test_job_completes_when_the_agent_reports_a_garbage_duration(client):
    from tests.test_phase3_marketplace import create_listable_machine
    from tests.test_phase4_jobs import seed_echo_model
    from tests.test_phase5_billing import _customer, _run_job_to_completion
    from tests.helpers import auth_headers

    await seed_echo_model()
    m = await create_listable_machine(client, "p-garbage@x.com", hostname="garbage")
    cust = await _customer(client, "c-garbage@x.com")
    # "NaN" used to raise InvalidOperation out of settle_job: the result call 500'd and the job could never complete.
    await _run_job_to_completion(client, cust, m, minutes=120, seconds="NaN")
    invoices = (await client.get("/v1/invoices", headers=auth_headers(cust))).json()
    assert len(invoices) == 1 and invoices[0]["actual_total"] != "0.00"


# ---------------------------------------------------------------------------
# billing: reserved amount covers interactive sessions and is scoped to the customer
# ---------------------------------------------------------------------------
async def test_customer_reserved_includes_sessions_and_ignores_other_customers(db_engine):
    from app.services import billing as billing_svc

    sm = get_sessionmaker()
    mine, other = uuid.uuid4(), uuid.uuid4()
    async with sm() as db:
        external = await ledger.get_platform_account(db, "external")
        holding = await ledger.get_platform_account(db, "holding")
        a = await _acct(db, "customer_credits", "customer", mine)
        b = await _acct(db, "customer_credits", "customer", other)
        for acct, uid, key in ((a, mine, "a"), (b, other, "b")):
            await ledger.post_transaction(
                db, kind="credit_purchase", legs=[(external, "debit", 10_000), (acct, "credit", 10_000)],
                idempotency_key=f"fund-{key}",
            )
        sid = uuid.uuid4()
        await ledger.post_transaction(
            db, kind="hold", legs=[(a, "debit", 700), (holding, "credit", 700)], idempotency_key="sess-hold-a",
            reference_type="session", reference_id=str(sid), actor_user_id=mine,
        )
        await ledger.post_transaction(
            db, kind="hold", legs=[(a, "debit", 300), (holding, "credit", 300)], idempotency_key="job-hold-a",
            reference_type="job", reference_id=str(uuid.uuid4()), actor_user_id=mine,
        )
        await ledger.post_transaction(
            db, kind="hold", legs=[(b, "debit", 5_000), (holding, "credit", 5_000)], idempotency_key="job-hold-b",
            reference_type="job", reference_id=str(uuid.uuid4()), actor_user_id=other,
        )
        await db.commit()
        assert await billing_svc.customer_reserved_minor(db, mine) == 1_000
        assert await billing_svc.customer_reserved_minor(db, other) == 5_000
        assert await billing_svc.customer_reserved_minor(db, uuid.uuid4()) == 0


# ---------------------------------------------------------------------------
# spend limits
# ---------------------------------------------------------------------------
async def test_withdrawal_refund_does_not_reduce_period_spend(db_engine):
    """A failed token withdrawal is returned as a ledger 'refund'. It is not a reversal of spend and must not make
    the customer look like they have spent less (which would let them exceed their own budget)."""
    from app.services import spend_limits

    sm = get_sessionmaker()
    uid = uuid.uuid4()
    async with sm() as db:
        external = await ledger.get_platform_account(db, "external")
        holding = await ledger.get_platform_account(db, "holding")
        pending = await ledger.get_platform_account(db, "withdrawal_pending")
        cust = await _acct(db, "customer_credits", "customer", uid)
        await ledger.post_transaction(
            db, kind="credit_purchase", legs=[(external, "debit", 100_00), (cust, "credit", 100_00)],
            idempotency_key="fund",
        )
        # Real spend: a 40.00 hold that stays captured.
        await ledger.post_transaction(
            db, kind="hold", legs=[(cust, "debit", 40_00), (holding, "credit", 40_00)], idempotency_key="h",
            reference_type="job", reference_id=str(uuid.uuid4()), actor_user_id=uid,
        )
        # A withdrawal of 30.00 that is then refunded.
        wid = str(uuid.uuid4())
        await ledger.post_transaction(
            db, kind="payout_request", legs=[(cust, "debit", 30_00), (pending, "credit", 30_00)],
            idempotency_key="w-req", reference_type="withdrawal", reference_id=wid,
        )
        await ledger.post_transaction(
            db, kind="refund", legs=[(pending, "debit", 30_00), (cust, "credit", 30_00)],
            idempotency_key="w-ref", reference_type="withdrawal", reference_id=wid,
        )
        await db.commit()
        spent = await spend_limits.period_spend_minor(db, customer_id=uid, period="total")
        assert spent == 40_00  # used to be 10_00: the withdrawal refund was netted off the real spend


async def test_project_spend_counts_session_spend(db_engine):
    from app.models.sessions import ComputeSession
    from app.services import spend_limits

    sm = get_sessionmaker()
    uid, project = uuid.uuid4(), uuid.uuid4()
    async with sm() as db:
        external = await ledger.get_platform_account(db, "external")
        holding = await ledger.get_platform_account(db, "holding")
        cust = await _acct(db, "customer_credits", "customer", uid)
        await ledger.post_transaction(
            db, kind="credit_purchase", legs=[(external, "debit", 100_00), (cust, "credit", 100_00)],
            idempotency_key="fund",
        )
        from app.models.user import User
        from app.models.enums import UserStatus

        user = User(id=uid, email="spend-sess@x.io", password_hash="x", status=UserStatus.active, email_verified=True)
        db.add(user)
        await db.flush()
        sess = ComputeSession(
            customer_id=uid, project_id=project, image_digest="sha256:x", max_duration_seconds=3600,
            idle_timeout_seconds=300, max_price_per_gpu_hour=1, client_pubkey="k" * 64, held_minor=25_00,
        )
        db.add(sess)
        await db.flush()
        await ledger.post_transaction(
            db, kind="hold", legs=[(cust, "debit", 25_00), (holding, "credit", 25_00)], idempotency_key="sh",
            reference_type="session", reference_id=str(sess.id), actor_user_id=uid,
        )
        await db.commit()
        spent = await spend_limits.period_spend_minor(
            db, customer_id=uid, period="total", scope="project", scope_id=project
        )
        assert spent == 25_00  # sessions carry a project_id and are budget-checked, so they must be counted


# ---------------------------------------------------------------------------
# Stripe Connect payouts
# ---------------------------------------------------------------------------
class _CapturingConnect:
    """FakeConnectClient that remembers the currency it was asked to transfer in."""

    def __init__(self, fail=False):
        from tests.test_c3_provider_growth import FakeConnectClient

        self._inner = FakeConnectClient(payouts_enabled=True)
        self.mode = "test"
        self.fail = fail
        self.currencies: list[str] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def create_transfer(self, *, amount_minor, currency, destination, idempotency_key, metadata):
        self.currencies.append(currency)
        if self.fail:
            raise RuntimeError("stripe is down")
        return self._inner.create_transfer(
            amount_minor=amount_minor, currency=currency, destination=destination,
            idempotency_key=idempotency_key, metadata=metadata,
        )


@pytest.fixture
def capturing_connect(request):
    from app.main import app
    from app.services.connect_payouts import get_connect_client

    fake = _CapturingConnect(fail=getattr(request, "param", False))
    app.dependency_overrides[get_connect_client] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_connect_client, None)


async def test_connect_transfer_is_made_in_usd_not_the_ledger_label(client, capturing_connect):
    """The ledger's currency label is 'USDC_CREDITS'. It was lower-cased and sent to Stripe as the transfer currency,
    which Stripe rejects: no live payout could ever succeed."""
    from tests.helpers import auth_headers
    from tests.test_c3_provider_growth import _fund_earnings
    from tests.test_phase3_marketplace import create_listable_machine

    m = await create_listable_machine(client, "c-usd@example.com", price="1.50")
    await _fund_earnings("c-usd@example.com", "40.00")
    await client.post("/v1/provider/connect/onboarding", json={}, headers=auth_headers(m["access"]))
    r = await client.post(
        "/v1/provider/connect/payouts", json={"amount": "5.00", "idempotency_key": "usd-payout-0001"},
        headers=auth_headers(m["access"]),
    )
    assert r.status_code == 201, r.text
    assert capturing_connect.currencies == ["usd"]


@pytest.mark.parametrize("capturing_connect", [True], indirect=True)
async def test_connect_transfer_failure_is_502_and_leaves_the_earnings_untouched(client, capturing_connect):
    from tests.helpers import auth_headers
    from tests.test_c3_provider_growth import _fund_earnings, _provider_id
    from tests.test_phase3_marketplace import create_listable_machine

    m = await create_listable_machine(client, "c-fail502@example.com", price="1.50")
    provider_id = await _fund_earnings("c-fail502@example.com", "40.00")
    await client.post("/v1/provider/connect/onboarding", json={}, headers=auth_headers(m["access"]))
    r = await client.post(
        "/v1/provider/connect/payouts", json={"amount": "5.00", "idempotency_key": "fail-payout-0001"},
        headers=auth_headers(m["access"]),
    )
    assert r.status_code == 502, r.text
    async with get_sessionmaker()() as db:
        assert (await ledger.get_provider_account(db, provider_id)).balance_minor == 4000


async def test_partial_transfer_reversal_does_not_refund_the_whole_payout(client, capturing_connect):
    import json
    from unittest import mock

    from app.core.config import settings
    from tests.helpers import auth_headers
    from tests.test_c3_provider_growth import TEST_WEBHOOK_SECRET, _fund_earnings, _stripe_sig
    from tests.test_phase3_marketplace import create_listable_machine

    m = await create_listable_machine(client, "c-partial@example.com", price="1.50")
    provider_id = await _fund_earnings("c-partial@example.com", "30.00")
    await client.post("/v1/provider/connect/onboarding", json={}, headers=auth_headers(m["access"]))
    paid = await client.post(
        "/v1/provider/connect/payouts", json={"amount": "12.00", "idempotency_key": "partial-0001"},
        headers=auth_headers(m["access"]),
    )
    assert paid.status_code == 201, paid.text
    event = {
        "id": "evt_partial_1", "type": "transfer.reversed",
        "data": {"object": {"id": "tr_x", "reversed": False, "amount_reversed": 100,
                            "metadata": {"payout_id": paid.json()["payout_id"]}}},
    }
    payload = json.dumps(event).encode()
    with mock.patch.object(settings, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
        resp = await client.post(
            "/v1/provider/connect/webhook", content=payload,
            headers={"stripe-signature": _stripe_sig(payload), "content-type": "application/json"},
        )
    assert resp.status_code == 200 and resp.json()["status"] == "partial_reversal_needs_review"
    async with get_sessionmaker()() as db:
        assert (await ledger.get_provider_account(db, provider_id)).balance_minor == 1800  # 30.00 - 12.00, no refund


# ---------------------------------------------------------------------------
# Stripe checkout webhooks and the simulated top-up
# ---------------------------------------------------------------------------
async def _post_stripe(client, event):
    import json
    from unittest import mock

    from app.core.config import settings
    from tests.test_stripe_billing import TEST_WEBHOOK_SECRET, _generate_stripe_signature

    payload = json.dumps(event).encode()
    with mock.patch.object(settings, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
        return await client.post(
            "/v1/billing/webhooks/stripe", content=payload,
            headers={"Content-Type": "application/json", "Stripe-Signature": _generate_stripe_signature(payload)},
        )


async def _available(client, token) -> str:
    from tests.helpers import auth_headers

    return (await client.get("/v1/billing/account", headers=auth_headers(token))).json()["available"]


async def test_one_stripe_purchase_is_credited_once_even_when_both_events_name_the_customer(client):
    """checkout.session.completed and payment_intent.succeeded are two events (two ids) for ONE payment."""
    from tests.helpers import get_user_id, register_verify_login

    token = (await register_verify_login(client, "dup-pi@example.com"))["access_token"]
    uid = await get_user_id("dup-pi@example.com")
    before = await _available(client, token)
    pi = "pi_dup_1"
    first = await _post_stripe(client, {
        "id": "evt_dup_a", "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_dup", "amount_total": 5000, "currency": "usd", "payment_intent": pi,
                            "payment_status": "paid", "client_reference_id": str(uid), "metadata": {"customer_id": str(uid)}}},
    })
    assert first.json()["status"] == "credited"
    second = await _post_stripe(client, {
        "id": "evt_dup_b", "type": "payment_intent.succeeded",
        "data": {"object": {"id": pi, "amount": 5000, "currency": "usd", "metadata": {"customer_id": str(uid)}}},
    })
    assert second.status_code == 200 and second.json()["status"] == "duplicate_payment_intent"
    import decimal

    assert decimal.Decimal(await _available(client, token)) == decimal.Decimal(before) + decimal.Decimal("50.00")


async def test_unpaid_or_foreign_currency_checkout_is_not_credited(client):
    from tests.helpers import get_user_id, register_verify_login

    token = (await register_verify_login(client, "unpaid@example.com"))["access_token"]
    uid = await get_user_id("unpaid@example.com")
    before = await _available(client, token)
    unpaid = await _post_stripe(client, {
        "id": "evt_unpaid", "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_u", "amount_total": 5000, "currency": "usd", "payment_intent": "pi_u",
                            "payment_status": "unpaid", "client_reference_id": str(uid)}},
    })
    assert unpaid.json()["status"] == "payment_unpaid"
    yen = await _post_stripe(client, {
        "id": "evt_yen", "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_y", "amount_total": 5000, "currency": "jpy", "payment_intent": "pi_y",
                            "payment_status": "paid", "client_reference_id": str(uid)}},
    })
    assert yen.json()["status"] == "unsupported_currency"
    assert await _available(client, token) == before


async def test_failed_payment_event_with_a_malformed_customer_id_is_acknowledged_and_not_duplicated(client):
    from sqlalchemy import func, select

    from app.models.billing import Payment

    bad = await _post_stripe(client, {
        "id": "evt_badref", "type": "payment_intent.payment_failed",
        "data": {"object": {"id": "pi_bad", "amount": 100, "metadata": {"customer_id": "not-a-uuid"}}},
    })
    assert bad.status_code == 200  # was 500: Stripe would have redelivered it for days
    from tests.helpers import get_user_id, register_verify_login

    await register_verify_login(client, "failreplay@example.com")
    uid = await get_user_id("failreplay@example.com")
    event = {"id": "evt_failreplay", "type": "payment_intent.payment_failed",
             "data": {"object": {"id": "pi_fr", "amount": 100, "metadata": {"customer_id": str(uid)}}}}
    for _ in range(2):
        assert (await _post_stripe(client, event)).status_code == 200
    async with get_sessionmaker()() as db:
        n = (await db.execute(select(func.count()).select_from(Payment).where(Payment.external_reference == "evt_failreplay"))).scalar_one()
    assert n == 1


async def test_simulated_topup_is_refused_when_stripe_is_the_payment_rail(client, monkeypatch):
    from app.core.config import settings
    from tests.helpers import auth_headers, register_verify_login

    token = (await register_verify_login(client, "mint@example.com"))["access_token"]
    before = await _available(client, token)
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "stripe")
    r = await client.post("/v1/billing/credits/purchase", json={"amount": "500.00"}, headers=auth_headers(token))
    assert r.status_code == 409 and r.json()["detail"] == "simulated_topup_disabled_use_checkout"
    assert await _available(client, token) == before
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "simulated")
    ok = await client.post("/v1/billing/credits/purchase", json={"amount": "5.00"}, headers=auth_headers(token))
    assert ok.status_code == 200


# ---------------------------------------------------------------------------
# Token withdrawals
# ---------------------------------------------------------------------------
@pytest.fixture
def wd_configured(monkeypatch):
    """The same withdrawal settings tests/test_b1_withdrawals.py applies (that fixture is module-local autouse)."""
    from app.core.config import settings
    from tests.test_b1_withdrawals import HOT, TOKEN

    for k, v in dict(
        WITHDRAWALS_ENABLED=True, CHAIN_RPC_URL="http://127.0.0.1:1", WITHDRAWALS_KMS_KEY_ID="k",
        WITHDRAWALS_HOT_WALLET_ADDRESS=HOT.address, COMPUTE_TOKEN_ADDRESS=TOKEN, TOKEN_CREDITS_PER_TOKEN="10",
        WITHDRAWALS_MIN_CREDITS="5.00", WITHDRAWALS_MAX_PER_TX_CREDITS="50.00", WITHDRAWALS_DAILY_USER_CREDITS="80.00",
        WITHDRAWALS_DAILY_GLOBAL_CREDITS="500.00", WITHDRAWALS_CONFIRMATIONS=2, WITHDRAWALS_TIMEOUT_SECONDS=3600,
        BILLING_SIGNUP_GRANT_MINOR=100_00,
    ).items():
        monkeypatch.setattr(settings, k, v)


async def test_timeout_does_not_refund_a_transfer_that_mined_between_the_receipt_and_nonce_reads(client, monkeypatch, wd_configured):
    """No receipt + nonce advanced is read as "another transaction took the nonce". But the nonce also advances when OUR
    transaction mines between those two reads; refunding then pays the user twice."""
    import datetime as dt

    from app.core.config import settings
    from app.models.withdrawal import Withdrawal
    from tests.test_b1_withdrawals import FakeChain, _cycle, _get, _request, _user_with_wallet

    uid, addr, _ = await _user_with_wallet(client, "race-wd@x.io")
    wd, _ = await _request(uid, addr)
    chain = FakeChain()
    await _cycle(chain)
    monkeypatch.setattr(settings, "WITHDRAWALS_TIMEOUT_SECONDS", 60)
    async with get_sessionmaker()() as db:
        w = await db.get(Withdrawal, wd.id)
        w.broadcast_at = w.broadcast_at - dt.timedelta(hours=1)
        tx_hash = w.tx_hash
        await db.commit()

    class MinesMidway(FakeChain):
        calls = 0

        async def receipt(self, h):
            MinesMidway.calls += 1
            return None if MinesMidway.calls == 1 else {"status": 1, "block_number": 100}

    racing = MinesMidway()
    racing.used_nonce = 1  # our own transaction just consumed nonce 0
    racing.latest = 100
    await _cycle(racing)
    w = await _get(wd.id)
    assert w.status == "broadcast", "must not refund: the transfer mined"
    assert tx_hash == w.tx_hash


async def test_reconciliation_flags_a_refunded_withdrawal_whose_transfer_is_on_chain(client, wd_configured):
    from app.models.withdrawal import Withdrawal
    from app.services import chain_reconciler
    from tests.test_b1_withdrawals import HOT, FakeChain, _cycle, _get, _request, _user_with_wallet

    uid, addr, _ = await _user_with_wallet(client, "double-pay@x.io")
    wd, _ = await _request(uid, addr)
    chain = FakeChain()
    await _cycle(chain)
    w = await _get(wd.id)
    async with get_sessionmaker()() as db:  # the credits were refunded (e.g. a wrongly-judged timeout) ...
        row = await db.get(Withdrawal, wd.id)
        await svc_refund(db, row)
        await db.commit()
    chain.latest = 500  # ... but the transfer DID mine and is confirmed on chain
    chain.logs = [{"tx_hash": w.tx_hash, "log_index": 0, "block_number": 50, "from_address": HOT.address.lower(),
                   "to_address": addr, "amount": int(w.amount_raw)}]
    async with get_sessionmaker()() as db:
        out = await chain_reconciler.reconcile_withdrawals(db, chain)
        await db.commit()
    assert out["ok"] is False
    assert [f["kind"] for f in out["findings"]] == ["refunded_but_paid_on_chain"]


async def svc_refund(db, wd):
    from app.services import withdrawals as svc

    await svc.refund_withdrawal(db, wd, "timeout_nonce_used")


# ---------------------------------------------------------------------------
# On-chain settlement vouchers
# ---------------------------------------------------------------------------
async def _escrow(client, db_engine, tag):
    from tests.test_phase10_settlement import _create_job_setup, _create_test_user, _lock_payload
    from app.models.enums import Role

    customer, c_token = await _create_test_user(db_engine, f"cust_{tag}@test.com", Role.customer)
    provider, _ = await _create_test_user(db_engine, f"prov_{tag}@test.com", Role.provider)
    _admin, a_token = await _create_test_user(db_engine, f"admin_{tag}@test.com", Role.administrator)
    job = await _create_job_setup(db_engine, customer, provider)
    ch, ah = {"Authorization": f"Bearer {c_token}"}, {"Authorization": f"Bearer {a_token}"}
    r = await client.post("/v1/settlement/escrows", json=_lock_payload(job, provider, "0x" + tag[0] * 64, 50 * 10**18), headers=ch)
    assert r.status_code == 201, r.text
    return job, ch, ah


@pytest.mark.parametrize(
    "final_cost,fee",
    [("abc", "1"), ("-5", "1"), ("10", "-1"), ("1.5", "0"), ("", "0"), (str(2**256), "0"), ("١٢", "0")],
)
async def test_voucher_request_with_malformed_amounts_is_422_not_500(client, db_engine, final_cost, fee):
    job, _ch, ah = await _escrow(client, db_engine, "mal")
    r = await client.post("/v1/settlement/vouchers", json={
        "job_id": str(job.id), "final_cost_units": final_cost, "platform_fee_units": fee}, headers=ah)
    assert r.status_code == 422, r.text


async def test_voucher_with_a_fee_above_the_contract_cap_is_refused_and_does_not_burn_the_escrow(client, db_engine):
    """settleJob reverts with 'Fee above cap' over 30%. Such a voucher used to be signed and stored, and the one-voucher
    rule then blocked the corrected re-issue, stranding the escrow."""
    job, _ch, ah = await _escrow(client, db_engine, "cap")
    bad = await client.post("/v1/settlement/vouchers", json={
        "job_id": str(job.id), "final_cost_units": str(40 * 10**18), "platform_fee_units": str(13 * 10**18)}, headers=ah)
    assert bad.status_code == 400, bad.text
    ok = await client.post("/v1/settlement/vouchers", json={
        "job_id": str(job.id), "final_cost_units": str(40 * 10**18), "platform_fee_units": str(12 * 10**18)}, headers=ah)
    assert ok.status_code == 200, ok.text  # exactly 30% is allowed by the contract


async def test_lock_with_malformed_amount_or_address_is_422(client, db_engine):
    from tests.test_phase10_settlement import _create_job_setup, _create_test_user, _lock_payload
    from app.models.enums import Role

    customer, c_token = await _create_test_user(db_engine, "cust_badlock@test.com", Role.customer)
    provider, _ = await _create_test_user(db_engine, "prov_badlock@test.com", Role.provider)
    job = await _create_job_setup(db_engine, customer, provider)
    ch = {"Authorization": f"Bearer {c_token}"}
    for patch in ({"locked_amount_units": "lots"}, {"locked_amount_units": "0"}, {"customer_address": "0x" + "z" * 40}):
        body = {**_lock_payload(job, provider, "0x" + "b" * 64), **patch}
        r = await client.post("/v1/settlement/escrows", json=body, headers=ch)
        assert r.status_code == 422, (patch, r.text)


async def test_confirm_settlement_rejects_an_escrow_that_was_already_refunded(client, db_engine):
    job, _ch, ah = await _escrow(client, db_engine, "ref")
    refunded = await client.post("/v1/settlement/refund", json={
        "job_id": str(job.id), "refund_tx_hash": "0x" + "f" * 64, "block_number": 2}, headers=ah)
    assert refunded.status_code == 200, refunded.text
    settled = await client.post("/v1/settlement/confirm", json={
        "job_id": str(job.id), "settle_tx_hash": "0x" + "d" * 64, "block_number": 3}, headers=ah)
    assert settled.status_code == 409, settled.text


# ---------------------------------------------------------------------------
# Quotes / reservations
# ---------------------------------------------------------------------------
async def test_quote_max_authorized_spend_beyond_the_column_is_422(client):
    from tests.helpers import auth_headers, register_verify_login
    from tests.test_phase3_marketplace import create_listable_machine

    m = await create_listable_machine(client, "p-quote-cap@example.com", hostname="qcap")
    token = (await register_verify_login(client, "c-quote-cap@example.com"))["access_token"]
    r = await client.post(
        "/v1/quotes",
        json={"machine_id": m["machine_id"], "workload_type": "text-generation", "estimated_minutes": 60,
              "max_authorized_spend": "1000000000000"},
        headers=auth_headers(token),
    )
    assert r.status_code == 422, r.text
    assert "max_authorized_spend" in r.text
    ok = await client.post(
        "/v1/quotes",
        json={"machine_id": m["machine_id"], "workload_type": "text-generation", "estimated_minutes": 60,
              "max_authorized_spend": "999999999999"},
        headers=auth_headers(token),
    )
    assert ok.status_code == 201, ok.text


async def test_reservation_backing_a_running_job_cannot_be_released(client):
    from tests.helpers import auth_headers
    from tests.test_phase3_marketplace import create_listable_machine
    from tests.test_phase4_jobs import claim, seed_echo_model, submit_job
    from tests.test_phase5_billing import _customer

    await seed_echo_model()
    m = await create_listable_machine(client, "p-resrel@example.com", hostname="resrel")
    cust = await _customer(client, "c-resrel@example.com")
    sub = await submit_job(client, cust, estimated_minutes=60)
    assert sub.status_code == 201, sub.text
    reservation_id = sub.json()["reservation_id"]
    await claim(client, m["machine_id"], m["secret"])  # job is now running on the reserved slot
    r = await client.post(f"/v1/reservations/{reservation_id}/release", json={}, headers=auth_headers(cust))
    assert r.status_code == 409 and r.json()["detail"] == "reservation_in_use"
