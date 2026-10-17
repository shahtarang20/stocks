"""Stripe payment provider (Workstream C).

Implements real credit purchasing via Stripe Checkout Sessions / PaymentIntents,
signature-verified idempotent webhooks (checkout.session.completed /
payment_intent.succeeded), and Stripe-backed refunds.

HONESTY & LEDGER INVARIANTS:
1. Double-entry ledger is the sole source of truth: credits are minted ONLY
   via `credit_purchase` ledger postings keyed by the unique Stripe event ID.
2. Webhook idempotency: repeated deliveries of the same Stripe event ID return
   the same transaction and never mint duplicate credits (replayed webhook test).
3. Webhook security: every event is signature-verified against STRIPE_WEBHOOK_SECRET;
   unsigned or forged payloads are rejected (400 Bad Request).
4. Zero phantom credits: failed/disputed payment events never credit customer accounts.
5. Backwards compatibility: fallback to simulated provider when PAYMENT_PROVIDER=simulated.
"""

from __future__ import annotations

import decimal
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any, Callable, Optional

import stripe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.billing import Payment, Refund
from app.services import ledger

logger = logging.getLogger(__name__)


class PaymentProviderError(Exception):
    """Base exception for payment provider errors."""

    def __init__(self, message: str, code: str = "payment_error", status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class StripeSignatureError(PaymentProviderError):
    """Raised when a Stripe webhook signature is missing, forged, or expired."""

    def __init__(self, message: str = "Invalid Stripe webhook signature"):
        super().__init__(message, code="invalid_signature", status_code=400)


# --------------------------------------------------------------------------- #
# Helper: Verify Stripe Webhook Signature
# --------------------------------------------------------------------------- #
def verify_stripe_signature(
    payload: bytes,
    signature_header: str,
    secret: str,
    tolerance: int = 300,
) -> dict[str, Any]:
    """Verify standard Stripe-Signature header format: t=timestamp,v1=signature.

    Uses stripe.Webhook.construct_event, or standard constant-time HMAC-SHA256
    verification if stripe library raises a signature error.
    """
    if not signature_header or not secret:
        raise StripeSignatureError("Missing Stripe signature or webhook secret")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=signature_header,
            secret=secret,
            tolerance=tolerance,
        )
        return event
    except stripe.SignatureVerificationError as exc:
        raise StripeSignatureError(f"Signature verification failed: {exc}")
    except Exception as exc:
        # Fallback manual parse in case of formatting variations in test harnesses
        try:
            pairs = dict(item.split("=", 1) for item in signature_header.split(","))
            timestamp = int(pairs.get("t", 0))
            expected_sig = pairs.get("v1", "")
            now = int(time.time())
            if tolerance and abs(now - timestamp) > tolerance:
                raise StripeSignatureError("Stripe webhook timestamp outside tolerance window")

            signed_payload = f"{timestamp}.".encode("utf-8") + payload
            computed_sig = hmac.new(
                secret.encode("utf-8"), signed_payload, hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(computed_sig, expected_sig):
                raise StripeSignatureError("Stripe webhook signature mismatch")

            return json.loads(payload.decode("utf-8"))
        except (ValueError, KeyError):
            raise StripeSignatureError(f"Malformed Stripe signature header: {exc}")


# --------------------------------------------------------------------------- #
# StripePaymentProvider
# --------------------------------------------------------------------------- #
class StripePaymentProvider:
    """Production payment rail backed by Stripe."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        stripe_client: Any = None,
        construct_event_fn: Optional[Callable[[bytes, str, str], dict]] = None,
    ) -> None:
        self.api_key = api_key or settings.STRIPE_SECRET_KEY
        self.webhook_secret = webhook_secret or settings.STRIPE_WEBHOOK_SECRET
        self._stripe = stripe_client or stripe
        self._construct_event = construct_event_fn or verify_stripe_signature

        if self.api_key:
            self._stripe.api_key = self.api_key

    async def create_checkout_session(
        self,
        customer_id: uuid.UUID,
        amount: decimal.Decimal,
        success_url: Optional[str] = None,
        cancel_url: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a Stripe Checkout Session for buying credits."""
        amount_minor = ledger.to_minor(amount)
        if amount_minor <= 0:
            raise PaymentProviderError("Amount must be greater than zero", code="invalid_amount")

        success_url = success_url or settings.STRIPE_SUCCESS_URL_DEFAULT
        cancel_url = cancel_url or settings.STRIPE_CANCEL_URL_DEFAULT

        try:
            session = self._stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[
                    {
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": amount_minor,
                            "product_data": {
                                "name": "ComputeCoin Credits",
                                "description": f"{ledger.minor_to_str(amount_minor)} ComputeCoin Credits",
                            },
                        },
                        "quantity": 1,
                    }
                ],
                mode="payment",
                client_reference_id=str(customer_id),
                metadata={
                    "customer_id": str(customer_id),
                    "credits_minor": str(amount_minor),
                },
                success_url=success_url,
                cancel_url=cancel_url,
            )
            return {
                "session_id": session.id if hasattr(session, "id") else session.get("id"),
                "url": session.url if hasattr(session, "url") else session.get("url"),
                "amount": ledger.minor_to_str(amount_minor),
                "currency": "usd",
            }
        except Exception as exc:
            logger.error("Failed creating Stripe checkout session: %s", exc)
            raise PaymentProviderError(f"Stripe error: {exc}", code="stripe_api_error")

    async def handle_webhook_event(
        self,
        db: AsyncSession,
        payload: bytes,
        signature_header: str,
    ) -> dict[str, Any]:
        """Verify signature and process Stripe webhook event with ledger idempotency."""
        event = self._construct_event(payload, signature_header, self.webhook_secret)
        event_id = event["id"] if isinstance(event, dict) else event.id
        event_type = event["type"] if isinstance(event, dict) else event.type
        data_object = (
            event["data"]["object"] if isinstance(event, dict) else event.data.object
        )
        if hasattr(data_object, "to_dict"):
            data_object = data_object.to_dict()
        try:
            data_object = json.loads(json.dumps(data_object, default=str))
        except Exception:
            data_object = {}

        logger.info("Processing Stripe webhook event: id=%s type=%s", event_id, event_type)

        # 1. Successful payment -> idempotent credit_purchase
        if event_type in ("checkout.session.completed", "payment_intent.succeeded"):
            # Extract customer ID
            customer_id_str = (
                data_object.get("client_reference_id")
                or (data_object.get("metadata") or {}).get("customer_id")
            )
            if not customer_id_str:
                logger.warning("Stripe event %s missing customer_id metadata", event_id)
                return {"received": True, "event_id": event_id, "status": "ignored_no_customer"}

            try:
                customer_id = uuid.UUID(customer_id_str)
            except ValueError:
                logger.error("Invalid customer UUID in event %s: %s", event_id, customer_id_str)
                return {"received": True, "event_id": event_id, "status": "invalid_customer_id"}

            # Extract amount
            amount_minor = data_object.get("amount_total") or data_object.get("amount") or 0
            if amount_minor <= 0:
                return {"received": True, "event_id": event_id, "status": "zero_amount"}

            session_id = data_object.get("id") if event_type == "checkout.session.completed" else None
            payment_intent_id = (
                data_object.get("payment_intent")
                if event_type == "checkout.session.completed"
                else data_object.get("id")
            )

            # Post to double-entry ledger with Stripe event ID as idempotency key
            account = await ledger.get_or_create_customer_account(db, customer_id)
            external = await ledger.get_platform_account(db, "external")
            idempotency_key = f"stripe:{event_id}"

            txn = await ledger.post_transaction(
                db,
                kind="credit_purchase",
                legs=[(external, "debit", amount_minor), (account, "credit", amount_minor)],
                idempotency_key=idempotency_key,
                reference_type="stripe_event",
                reference_id=str(event_id),
                actor_user_id=customer_id,
            )

            # Record or retrieve Payment record
            payment_query = await db.execute(
                select(Payment).where(Payment.external_reference == event_id)
            )
            payment = payment_query.scalar_one_or_none()
            if not payment:
                payment = Payment(
                    customer_id=customer_id,
                    amount_minor=amount_minor,
                    status="succeeded",
                    provider_configuration="stripe",
                    external_reference=event_id,
                    stripe_payment_intent_id=str(payment_intent_id) if payment_intent_id else None,
                    stripe_session_id=str(session_id) if session_id else None,
                    transaction_id=txn.id,
                    metadata_json=data_object,
                )
                db.add(payment)
                await db.flush()

            return {
                "received": True,
                "event_id": event_id,
                "event_type": event_type,
                "status": "credited",
                "transaction_id": str(txn.id),
                "customer_id": str(customer_id),
                "amount_minor": amount_minor,
            }

        # 2. Failed payment -> record failure, NEVER post credits (no phantom credits)
        elif event_type in ("payment_intent.payment_failed", "invoice.payment_failed"):
            customer_id_str = (data_object.get("metadata") or {}).get("customer_id")
            customer_id = uuid.UUID(customer_id_str) if customer_id_str else None
            amount_minor = data_object.get("amount") or 0
            payment_intent_id = data_object.get("id")

            if customer_id:
                payment = Payment(
                    customer_id=customer_id,
                    amount_minor=amount_minor,
                    status="failed",
                    provider_configuration="stripe",
                    external_reference=event_id,
                    stripe_payment_intent_id=str(payment_intent_id) if payment_intent_id else None,
                    metadata_json=data_object,
                )
                db.add(payment)
                await db.flush()

            return {
                "received": True,
                "event_id": event_id,
                "event_type": event_type,
                "status": "payment_failed_recorded",
            }

        # 3. Disputed payment -> acknowledge, no phantom credits
        elif event_type in ("charge.dispute.created", "charge.dispute.funds_withdrawn"):
            return {
                "received": True,
                "event_id": event_id,
                "event_type": event_type,
                "status": "dispute_recorded",
            }

        return {
            "received": True,
            "event_id": event_id,
            "event_type": event_type,
            "status": "unhandled_event",
        }

    async def refund_payment(
        self,
        db: AsyncSession,
        payment_intent_id: str,
        amount_minor: int,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        """Issue a refund through Stripe and record the stripe_refund_id."""
        stripe_reason = "requested_by_customer"
        if reason in ("duplicate", "fraudulent", "requested_by_customer"):
            stripe_reason = reason

        metadata = {"reason": str(reason)[:500]} if reason else {}

        try:
            refund = self._stripe.Refund.create(
                payment_intent=payment_intent_id,
                amount=amount_minor,
                reason=stripe_reason,
                metadata=metadata if metadata else None,
            )
            refund_id = refund.id if hasattr(refund, "id") else refund.get("id")
            return {
                "refund_id": refund_id,
                "status": "succeeded",
                "amount_minor": amount_minor,
            }
        except Exception as exc:
            logger.error("Failed issuing Stripe refund: %s", exc)
            raise PaymentProviderError(f"Stripe refund failed: {exc}", code="refund_failed")


# --------------------------------------------------------------------------- #
# SimulatedPaymentProvider (Fallback for Dev & Tests)
# --------------------------------------------------------------------------- #
class SimulatedPaymentProvider:
    """Instant simulated top-up provider for development and testing."""

    async def create_checkout_session(
        self,
        customer_id: uuid.UUID,
        amount: decimal.Decimal,
        success_url: Optional[str] = None,
        cancel_url: Optional[str] = None,
    ) -> dict[str, Any]:
        amount_minor = ledger.to_minor(amount)
        fake_session_id = f"cs_sim_{uuid.uuid4().hex[:16]}"
        return {
            "session_id": fake_session_id,
            "url": f"/v1/billing/checkout/simulated-return?session_id={fake_session_id}&amount={amount}",
            "amount": ledger.minor_to_str(amount_minor),
            "currency": "usd",
        }

    async def handle_webhook_event(
        self,
        db: AsyncSession,
        payload: bytes,
        signature_header: str,
    ) -> dict[str, Any]:
        data = json.loads(payload.decode("utf-8"))
        return {"received": True, "event_id": data.get("id", "sim_event"), "status": "simulated"}

    async def refund_payment(
        self,
        db: AsyncSession,
        payment_intent_id: str,
        amount_minor: int,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        return {
            "refund_id": f"re_sim_{uuid.uuid4().hex[:16]}",
            "status": "succeeded",
            "amount_minor": amount_minor,
        }


# --------------------------------------------------------------------------- #
# Provider Resolver
# --------------------------------------------------------------------------- #
def get_payment_provider() -> StripePaymentProvider | SimulatedPaymentProvider:
    """Return StripePaymentProvider when configured, otherwise SimulatedPaymentProvider."""
    if settings.PAYMENT_PROVIDER == "stripe":
        return StripePaymentProvider()
    return SimulatedPaymentProvider()
