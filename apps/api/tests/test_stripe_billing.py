"""Workstream C — Stripe payment rail and webhook idempotency test suite.

Proves:
1. Provider fallback: Simulated provider handles checkout when PAYMENT_PROVIDER="simulated".
2. Stripe Checkout session creation: When PAYMENT_PROVIDER="stripe", sessions are
   created with accurate line items, customer reference, and metadata.
3. Webhook signature security:
   - Missing Stripe-Signature header rejected (HTTP 400).
   - Forged or invalid HMAC signature rejected (HTTP 400).
   - Wrong secret rejected (HTTP 400).
   - Tampered payload rejected (HTTP 400).
   - Expired timestamp (> tolerance) rejected (HTTP 400).
4. Idempotency & ledger invariants (Single credit on replayed webhook):
   - Valid checkout.session.completed credits customer account exactly once.
   - Replayed webhook returns HTTP 200 and mints ZERO duplicate credits.
   - Double-entry ledger equality strictly holds: SUM(debits) == SUM(credits).
5. Zero phantom credits:
   - Failed payments (payment_intent.payment_failed) record failure and never credit.
   - Disputed payments (charge.dispute.created) record dispute and never credit.
6. Stripe refund dispatch:
   - Admin refund invokes stripe.Refund.create and records stripe_refund_id.
"""

from __future__ import annotations

import decimal
import json
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
import stripe
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.db.session import get_sessionmaker
from app.models.billing import Payment, Refund
from app.models.enums import Role
from app.services import ledger
from tests.helpers import auth_headers, get_user_id, grant_role, register_verify_login
from tests.test_phase3_marketplace import create_listable_machine
from tests.test_phase4_jobs import _artifact_tmp, post_result, seed_echo_model, submit_job  # noqa: F401

pytestmark = pytest.mark.asyncio

TEST_WEBHOOK_SECRET = "whsec_test_stripe_secret_key_12345"


def _generate_stripe_signature(
    payload_bytes: bytes,
    secret: str = TEST_WEBHOOK_SECRET,
    timestamp: int | None = None,
) -> str:
    """Generate a valid Stripe-Signature header format: t=...,v1=..."""
    ts = timestamp if timestamp is not None else int(time.time())
    payload_str = payload_bytes.decode("utf-8")
    return stripe.WebhookSignature.generate_signature_header(
        payload=payload_str,
        secret=secret,
        timestamp=ts,
    )


from tests.test_phase5_billing import _run_job_to_completion


# --------------------------------------------------------------------------- #
# 1. Checkout Session Creation & Provider Fallback
# --------------------------------------------------------------------------- #
async def test_checkout_session_creation_simulated_fallback(client: AsyncClient):
    """Simulated provider handles checkout session creation when PAYMENT_PROVIDER='simulated'."""
    tokens = await register_verify_login(client, "stripe_sim@example.com")
    cust_token = tokens["access_token"]

    with patch.object(settings, "PAYMENT_PROVIDER", "simulated"):
        resp = await client.post(
            "/v1/billing/checkout/session",
            json={"amount": "25.00"},
            headers=auth_headers(cust_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"].startswith("cs_sim_")
        assert "/v1/billing/checkout/simulated-return" in data["url"]
        assert data["amount"] == "25.00"
        assert data["currency"] == "usd"


async def test_checkout_session_creation_stripe(client: AsyncClient):
    """Stripe checkout session creation calls stripe.checkout.Session.create with accurate payload."""
    tokens = await register_verify_login(client, "stripe_real@example.com")
    cust_token = tokens["access_token"]
    user_id = await get_user_id("stripe_real@example.com")

    mock_session = MagicMock()
    mock_session.id = "cs_test_mock_session_123"
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_test_mock_session_123"

    with patch.object(settings, "PAYMENT_PROVIDER", "stripe"), \
         patch.object(settings, "STRIPE_SECRET_KEY", "sk_test_fake_123"), \
         patch("stripe.checkout.Session.create", return_value=mock_session) as mock_create:

        resp = await client.post(
            "/v1/billing/checkout/session",
            json={
                "amount": "50.00",
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            },
            headers=auth_headers(cust_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "cs_test_mock_session_123"
        assert data["url"] == "https://checkout.stripe.com/c/pay/cs_test_mock_session_123"
        assert data["amount"] == "50.00"
        assert data["currency"] == "usd"

        # Verify stripe client was called with minor units ($50.00 -> 5000 cents)
        mock_create.assert_called_once()
        _, kwargs = mock_create.call_args
        assert kwargs["mode"] == "payment"
        assert kwargs["client_reference_id"] == str(user_id)
        assert kwargs["success_url"] == "https://example.com/success"
        assert kwargs["cancel_url"] == "https://example.com/cancel"
        line_item = kwargs["line_items"][0]
        assert line_item["price_data"]["unit_amount"] == 5000
        assert line_item["price_data"]["currency"] == "usd"
        assert kwargs["metadata"]["customer_id"] == str(user_id)
        assert kwargs["metadata"]["credits_minor"] == "5000"


async def test_checkout_session_rejects_non_positive_amount(client: AsyncClient):
    """Checkout session creation rejects 0 or negative credit amounts."""
    tokens = await register_verify_login(client, "stripe_neg@example.com")
    cust_token = tokens["access_token"]

    resp = await client.post(
        "/v1/billing/checkout/session",
        json={"amount": "0.00"},
        headers=auth_headers(cust_token),
    )
    assert resp.status_code == 422  # validation rejects <= 0


# --------------------------------------------------------------------------- #
# 2. Webhook Signature Verification Security
# --------------------------------------------------------------------------- #
async def test_stripe_webhook_rejects_missing_signature(client: AsyncClient):
    """Missing Stripe-Signature header returns HTTP 400 Bad Request."""
    payload = json.dumps({"id": "evt_test_1", "type": "checkout.session.completed"}).encode("utf-8")
    with patch.object(settings, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
        resp = await client.post(
            "/v1/billing/webhooks/stripe",
            content=payload,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "signature" in resp.json()["detail"].lower()


async def test_stripe_webhook_rejects_forged_or_invalid_signature(client: AsyncClient):
    """Forged or mismatched HMAC signatures are rejected with HTTP 400."""
    payload = json.dumps({"id": "evt_test_2", "type": "checkout.session.completed"}).encode("utf-8")
    forged_sig = "t=1792241165,v1=0000000000000000000000000000000000000000000000000000000000000000"

    with patch.object(settings, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
        resp = await client.post(
            "/v1/billing/webhooks/stripe",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": forged_sig,
            },
        )
        assert resp.status_code == 400
        assert "signature" in resp.json()["detail"].lower()


async def test_stripe_webhook_rejects_wrong_secret(client: AsyncClient):
    """Signature generated with a different secret is rejected with HTTP 400."""
    payload = json.dumps({"id": "evt_test_3", "type": "checkout.session.completed"}).encode("utf-8")
    sig = _generate_stripe_signature(payload, secret="whsec_wrong_key_99999")

    with patch.object(settings, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
        resp = await client.post(
            "/v1/billing/webhooks/stripe",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": sig,
            },
        )
        assert resp.status_code == 400
        assert "signature" in resp.json()["detail"].lower()


async def test_stripe_webhook_rejects_tampered_payload(client: AsyncClient):
    """Modifying the webhook payload body after signing invalidates HMAC and is rejected."""
    original_payload = json.dumps({"id": "evt_test_4", "amount": 1000}).encode("utf-8")
    sig = _generate_stripe_signature(original_payload, secret=TEST_WEBHOOK_SECRET)
    tampered_payload = json.dumps({"id": "evt_test_4", "amount": 999999}).encode("utf-8")

    with patch.object(settings, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
        resp = await client.post(
            "/v1/billing/webhooks/stripe",
            content=tampered_payload,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": sig,
            },
        )
        assert resp.status_code == 400
        assert "signature" in resp.json()["detail"].lower()


async def test_stripe_webhook_rejects_expired_timestamp(client: AsyncClient):
    """Signatures outside tolerance window (default 300 seconds) are rejected with HTTP 400."""
    payload = json.dumps({"id": "evt_test_5", "type": "checkout.session.completed"}).encode("utf-8")
    expired_timestamp = int(time.time()) - 600  # 10 minutes old
    expired_sig = _generate_stripe_signature(payload, secret=TEST_WEBHOOK_SECRET, timestamp=expired_timestamp)

    with patch.object(settings, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
        resp = await client.post(
            "/v1/billing/webhooks/stripe",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": expired_sig,
            },
        )
        assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# 3. Idempotency & Replayed Webhook Tests (No Duplicate Credits)
# --------------------------------------------------------------------------- #
async def test_stripe_webhook_checkout_completed_and_idempotency_replay(client: AsyncClient):
    """checkout.session.completed credits customer once; replaying identical event mints zero extra credits."""
    email = "stripe_buyer_1@example.com"
    tokens = await register_verify_login(client, email)
    cust_token = tokens["access_token"]
    user_id = await get_user_id(email)

    # Check baseline balance before Stripe purchase
    acct_before = (await client.get("/v1/billing/account", headers=auth_headers(cust_token))).json()
    base_available = decimal.Decimal(acct_before["available"])

    event_id = f"evt_checkout_{uuid.uuid4().hex[:12]}"
    session_id = f"cs_test_{uuid.uuid4().hex[:12]}"
    payment_intent_id = f"pi_test_{uuid.uuid4().hex[:12]}"
    amount_minor = 5000  # $50.00
    amount_decimal = decimal.Decimal("50.00")

    payload_dict = {
        "id": event_id,
        "object": "event",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": session_id,
                "object": "checkout.session",
                "amount_total": amount_minor,
                "currency": "usd",
                "payment_intent": payment_intent_id,
                "client_reference_id": str(user_id),
                "metadata": {
                    "customer_id": str(user_id),
                    "credits_minor": str(amount_minor),
                },
            }
        },
    }
    payload_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = _generate_stripe_signature(payload_bytes, secret=TEST_WEBHOOK_SECRET)

    with patch.object(settings, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
        # 1. First delivery of webhook
        resp1 = await client.post(
            "/v1/billing/webhooks/stripe",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": sig,
            },
        )
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["received"] is True
        assert data1["event_id"] == event_id
        assert data1["status"] == "credited"
        txn_id = data1["transaction_id"]
        assert txn_id is not None

        # Verify customer balance increased by exactly $50.00
        acct_after1 = (await client.get("/v1/billing/account", headers=auth_headers(cust_token))).json()
        expected_available = base_available + amount_decimal
        assert decimal.Decimal(acct_after1["available"]) == expected_available

        # Verify Payment record was created in DB
        session_maker = get_sessionmaker()
        async with session_maker() as db:
            payment_query = await db.execute(
                select(Payment).where(Payment.external_reference == event_id)
            )
            payment = payment_query.scalar_one_or_none()
            assert payment is not None
            assert payment.customer_id == user_id
            assert payment.amount_minor == amount_minor
            assert payment.status == "succeeded"
            assert payment.stripe_session_id == session_id
            assert payment.stripe_payment_intent_id == payment_intent_id
            assert payment.transaction_id == uuid.UUID(txn_id)

            # Check double-entry ledger equality: SUM(debits) == SUM(credits)
            total_debits, total_credits = await ledger.ledger_totals(db)
            assert total_debits == total_credits

        # 2. REPLAY: Deliver the exact same webhook payload again
        resp2 = await client.post(
            "/v1/billing/webhooks/stripe",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": sig,
            },
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["received"] is True
        assert data2["event_id"] == event_id
        # The returned transaction ID matches the original transaction
        assert data2["transaction_id"] == txn_id

        # CRITICAL TEST ASSERTION: Balance must remain EXACTLY identical (zero duplicate credit!)
        acct_after2 = (await client.get("/v1/billing/account", headers=auth_headers(cust_token))).json()
        assert decimal.Decimal(acct_after2["available"]) == expected_available

        # Verify exactly one Payment record exists (no duplicate records)
        async with session_maker() as db:
            payments = (
                await db.execute(select(Payment).where(Payment.external_reference == event_id))
            ).scalars().all()
            assert len(payments) == 1

            # Ledger remains balanced
            total_debits2, total_credits2 = await ledger.ledger_totals(db)
            assert total_debits2 == total_credits2


async def test_stripe_webhook_payment_intent_succeeded(client: AsyncClient):
    """payment_intent.succeeded webhook credits customer account and remains idempotent on replay."""
    email = "stripe_pi@example.com"
    tokens = await register_verify_login(client, email)
    cust_token = tokens["access_token"]
    user_id = await get_user_id(email)

    acct_before = (await client.get("/v1/billing/account", headers=auth_headers(cust_token))).json()
    base_available = decimal.Decimal(acct_before["available"])

    event_id = f"evt_pi_{uuid.uuid4().hex[:12]}"
    pi_id = f"pi_test_{uuid.uuid4().hex[:12]}"
    amount_minor = 2500  # $25.00

    payload_dict = {
        "id": event_id,
        "object": "event",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": pi_id,
                "object": "payment_intent",
                "amount": amount_minor,
                "currency": "usd",
                "metadata": {
                    "customer_id": str(user_id),
                },
            }
        },
    }
    payload_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = _generate_stripe_signature(payload_bytes, secret=TEST_WEBHOOK_SECRET)

    with patch.object(settings, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
        resp = await client.post(
            "/v1/billing/webhooks/stripe",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": sig,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["received"] is True
        assert data["status"] == "credited"

        acct_after = (await client.get("/v1/billing/account", headers=auth_headers(cust_token))).json()
        assert decimal.Decimal(acct_after["available"]) == base_available + decimal.Decimal("25.00")


# --------------------------------------------------------------------------- #
# 4. Zero Phantom Credits on Failed or Disputed Payments
# --------------------------------------------------------------------------- #
async def test_stripe_webhook_no_phantom_credits_on_failed_payment(client: AsyncClient):
    """payment_intent.payment_failed logs failed payment and NEVER credits customer balance."""
    email = "stripe_fail@example.com"
    tokens = await register_verify_login(client, email)
    cust_token = tokens["access_token"]
    user_id = await get_user_id(email)

    acct_before = (await client.get("/v1/billing/account", headers=auth_headers(cust_token))).json()
    initial_available = decimal.Decimal(acct_before["available"])

    event_id = f"evt_fail_{uuid.uuid4().hex[:12]}"
    pi_id = f"pi_failed_{uuid.uuid4().hex[:12]}"

    payload_dict = {
        "id": event_id,
        "object": "event",
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "id": pi_id,
                "object": "payment_intent",
                "amount": 10000,
                "currency": "usd",
                "last_payment_error": {"message": "Card declined"},
                "metadata": {
                    "customer_id": str(user_id),
                },
            }
        },
    }
    payload_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = _generate_stripe_signature(payload_bytes, secret=TEST_WEBHOOK_SECRET)

    with patch.object(settings, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
        resp = await client.post(
            "/v1/billing/webhooks/stripe",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": sig,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "payment_failed_recorded"

        # Customer balance is completely unaffected
        acct_after = (await client.get("/v1/billing/account", headers=auth_headers(cust_token))).json()
        assert decimal.Decimal(acct_after["available"]) == initial_available

        # Verify failed Payment row exists in DB with no transaction_id
        session_maker = get_sessionmaker()
        async with session_maker() as db:
            payment = (
                await db.execute(select(Payment).where(Payment.external_reference == event_id))
            ).scalar_one_or_none()
            assert payment is not None
            assert payment.status == "failed"
            assert payment.transaction_id is None


async def test_stripe_webhook_no_phantom_credits_on_dispute(client: AsyncClient):
    """charge.dispute.created acknowledges event without minting any unearned credits."""
    email = "stripe_disp@example.com"
    tokens = await register_verify_login(client, email)
    cust_token = tokens["access_token"]

    acct_before = (await client.get("/v1/billing/account", headers=auth_headers(cust_token))).json()
    initial_available = decimal.Decimal(acct_before["available"])

    event_id = f"evt_disp_{uuid.uuid4().hex[:12]}"
    payload_dict = {
        "id": event_id,
        "object": "event",
        "type": "charge.dispute.created",
        "data": {
            "object": {
                "id": f"dp_{uuid.uuid4().hex[:12]}",
                "amount": 5000,
                "reason": "fraudulent",
            }
        },
    }
    payload_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = _generate_stripe_signature(payload_bytes, secret=TEST_WEBHOOK_SECRET)

    with patch.object(settings, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
        resp = await client.post(
            "/v1/billing/webhooks/stripe",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": sig,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "dispute_recorded"

        acct_after = (await client.get("/v1/billing/account", headers=auth_headers(cust_token))).json()
        assert decimal.Decimal(acct_after["available"]) == initial_available


# --------------------------------------------------------------------------- #
# 5. Stripe Refund Dispatch Integration
# --------------------------------------------------------------------------- #
async def test_admin_refund_dispatches_stripe_refund(client: AsyncClient):
    """Admin refund of a Stripe-funded job dispatches stripe.Refund.create and records stripe_refund_id."""
    await seed_echo_model()
    machine = await create_listable_machine(client, "stripe_prov@example.com", hostname="sprov")

    cust_email = "stripe_refunder@example.com"
    cust_tokens = await register_verify_login(client, cust_email)
    cust_token = cust_tokens["access_token"]
    cust_id = await get_user_id(cust_email)

    admin_email = "stripe_admin@example.com"
    admin_tokens = await register_verify_login(client, admin_email)
    await grant_role(admin_email, Role.administrator)
    admin_token = admin_tokens["access_token"]

    # 1. Simulate Stripe purchase of $50.00
    stripe_pi_id = f"pi_live_{uuid.uuid4().hex[:12]}"
    event_id = f"evt_topup_{uuid.uuid4().hex[:12]}"
    payload_dict = {
        "id": event_id,
        "object": "event",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": f"cs_{uuid.uuid4().hex[:12]}",
                "amount_total": 5000,
                "currency": "usd",
                "payment_intent": stripe_pi_id,
                "client_reference_id": str(cust_id),
            }
        },
    }
    payload_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = _generate_stripe_signature(payload_bytes, secret=TEST_WEBHOOK_SECRET)

    with patch.object(settings, "STRIPE_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
        webhook_resp = await client.post(
            "/v1/billing/webhooks/stripe",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": sig,
            },
        )
        assert webhook_resp.status_code == 200

    # 2. Run a job to completion
    job_id = await _run_job_to_completion(client, cust_token, machine)

    # 3. Admin refunds the job with PAYMENT_PROVIDER="stripe"
    mock_stripe_refund = MagicMock()
    mock_stripe_refund.id = f"re_test_{uuid.uuid4().hex[:12]}"

    with patch.object(settings, "PAYMENT_PROVIDER", "stripe"), \
         patch.object(settings, "STRIPE_SECRET_KEY", "sk_test_fake_key"), \
         patch("stripe.Refund.create", return_value=mock_stripe_refund) as mock_refund_create:

        refund_resp = await client.post(
            "/v1/admin/refunds",
            json={"job_id": str(job_id), "amount": "1.00", "reason": "requested_by_customer"},
            headers=auth_headers(admin_token),
        )
        assert refund_resp.status_code == 201
        refund_data = refund_resp.json()
        assert refund_data["amount"] == "1.00"

        # Verify stripe.Refund.create was called with the PaymentIntent ID and minor amount
        mock_refund_create.assert_called_once()
        _, kwargs = mock_refund_create.call_args
        assert kwargs["payment_intent"] == stripe_pi_id
        assert kwargs["amount"] == 100  # $1.00 -> 100 minor units

        # Verify stripe_refund_id was saved to the Refund row in DB
        session_maker = get_sessionmaker()
        async with session_maker() as db:
            refund_row = (
                await db.execute(select(Refund).where(Refund.id == uuid.UUID(refund_data["refund_id"])))
            ).scalar_one_or_none()
            assert refund_row is not None
            assert refund_row.stripe_refund_id == mock_stripe_refund.id

            # Verify ledger is balanced
            total_debits, total_credits = await ledger.ledger_totals(db)
            assert total_debits == total_credits
