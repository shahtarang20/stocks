"""Stripe Connect payouts — paying provider earnings out to the real world.

WHY this is its own module
--------------------------
``/v1/provider/payouts`` (Phase 5) only moves earnings into the platform's
``payout_pending`` liability account; an operator still had to be paid by hand.
Connect closes that loop. Because this is the one place where money LEAVES the
platform, every rule here is defensive:

* the ledger is still the only mover of money — a Connect payout is a
  :func:`app.services.ledger.post_transaction`, never a balance mutation;
* a payout can never exceed the provider's *available* earnings — the pre-check
  is advisory, the ledger's no-negative guard on ``provider_earnings`` is the
  real one;
* a payout is idempotent on a caller-supplied key, end to end: the ledger
  transaction, the ``provider_payouts`` row AND the Stripe transfer all reuse
  the same key, so a retried request pays once;
* no payout without ``payouts_enabled`` + ``details_submitted`` (Stripe's KYC
  verdict) — paying an unverified account is how platforms get shut down;
* webhook events are signature-verified with the same helper as the main
  payments rail; a forged event changes nothing.

Offline/test mode: the Stripe client is INJECTED (see :func:`get_connect_client`,
used as a FastAPI dependency), so tests run with a fake and never touch network.
When ``STRIPE_CONNECT_ENABLED`` is false the simulated client is used and every
response is stamped ``mode="simulated"`` — a simulated transfer is never
reported as a real one.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.billing import LedgerTransaction, ProviderPayout
from app.models.enterprise import ConnectAccount
from app.models.provider import Provider
from app.services import ledger
from app.services.audit import record_audit
from app.services.stripe_payment import StripeSignatureError, verify_stripe_signature

logger = logging.getLogger(__name__)

__all__ = [
    "ConnectError",
    "ConnectClient",
    "SimulatedConnectClient",
    "StripeConnectClient",
    "get_connect_client",
    "ensure_connect_account",
    "create_onboarding_link",
    "refresh_account_status",
    "available_earnings_minor",
    "create_connect_payout",
    "handle_connect_webhook",
]

# Terminal Stripe account states that must never receive a transfer.
_PAYABLE_STATUS = "active"


class ConnectError(Exception):
    """A refusal from the Connect payout rail (surfaces as an HTTP error)."""

    def __init__(self, code: str, status_code: int = 400, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.status_code = status_code
        self.message = message or code


# ---------------------------------------------------------------------------
# Client abstraction (injected — tests pass a fake, production passes Stripe)
# ---------------------------------------------------------------------------
class ConnectClient(Protocol):
    """The slice of Stripe Connect this service uses."""

    mode: str

    def create_account(self, *, provider_id: str, email: str | None) -> dict[str, Any]: ...

    def create_account_link(
        self, *, account_id: str, refresh_url: str, return_url: str
    ) -> dict[str, Any]: ...

    def retrieve_account(self, *, account_id: str) -> dict[str, Any]: ...

    def create_transfer(
        self,
        *,
        amount_minor: int,
        currency: str,
        destination: str,
        idempotency_key: str,
        metadata: dict[str, str],
    ) -> dict[str, Any]: ...


class SimulatedConnectClient:
    """Offline stand-in used in dev and when ``STRIPE_CONNECT_ENABLED`` is false.

    It never contacts Stripe. Everything it returns is prefixed ``*_sim_`` and
    the caller stamps ``mode="simulated"`` on the response so a simulated
    transfer is never mistaken for a real one.
    """

    mode = "simulated"

    def create_account(self, *, provider_id: str, email: str | None) -> dict[str, Any]:
        return {
            "id": f"acct_sim_{uuid.uuid5(uuid.NAMESPACE_URL, provider_id).hex[:16]}",
            "payouts_enabled": False,
            "details_submitted": False,
            "requirements": {"currently_due": ["external_account"]},
        }

    def create_account_link(
        self, *, account_id: str, refresh_url: str, return_url: str
    ) -> dict[str, Any]:
        return {
            "url": f"https://connect.simulated.local/onboarding/{account_id}",
            "expires_at": int(utcnow().timestamp()) + 300,
        }

    def retrieve_account(self, *, account_id: str) -> dict[str, Any]:
        # Dev convenience: simulated onboarding "completes" immediately.
        return {
            "id": account_id,
            "payouts_enabled": True,
            "details_submitted": True,
            "requirements": {"currently_due": []},
        }

    def create_transfer(
        self,
        *,
        amount_minor: int,
        currency: str,
        destination: str,
        idempotency_key: str,
        metadata: dict[str, str],
    ) -> dict[str, Any]:
        # Derived from the idempotency key so a replay yields the SAME id.
        return {
            "id": f"tr_sim_{uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key).hex[:20]}",
            "amount": amount_minor,
            "currency": currency,
            "destination": destination,
        }


class StripeConnectClient:
    """Real Stripe Connect calls (Express accounts + transfers)."""

    mode = "live"

    def __init__(self, api_key: str | None = None, stripe_module: Any = None) -> None:
        import stripe as _stripe

        self._stripe = stripe_module or _stripe
        self.api_key = api_key or settings.STRIPE_SECRET_KEY
        if self.api_key:
            self._stripe.api_key = self.api_key

    @staticmethod
    def _as_dict(obj: Any) -> dict[str, Any]:
        if isinstance(obj, dict):
            return obj
        if hasattr(obj, "to_dict"):
            return dict(obj.to_dict())
        return {k: getattr(obj, k) for k in ("id", "payouts_enabled", "details_submitted") if hasattr(obj, k)}

    def create_account(self, *, provider_id: str, email: str | None) -> dict[str, Any]:
        account = self._stripe.Account.create(
            type="express",
            email=email,
            capabilities={"transfers": {"requested": True}},
            metadata={"provider_id": provider_id},
        )
        return self._as_dict(account)

    def create_account_link(
        self, *, account_id: str, refresh_url: str, return_url: str
    ) -> dict[str, Any]:
        link = self._stripe.AccountLink.create(
            account=account_id,
            refresh_url=refresh_url,
            return_url=return_url,
            type="account_onboarding",
        )
        return self._as_dict(link) if isinstance(link, dict) else {"url": link.url, "expires_at": getattr(link, "expires_at", None)}

    def retrieve_account(self, *, account_id: str) -> dict[str, Any]:
        return self._as_dict(self._stripe.Account.retrieve(account_id))

    def create_transfer(
        self,
        *,
        amount_minor: int,
        currency: str,
        destination: str,
        idempotency_key: str,
        metadata: dict[str, str],
    ) -> dict[str, Any]:
        # Stripe's own idempotency key mirrors ours: a retried HTTP call after a
        # network failure must not create a second transfer.
        transfer = self._stripe.Transfer.create(
            amount=amount_minor,
            currency=currency,
            destination=destination,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
        return self._as_dict(transfer)


def get_connect_client() -> ConnectClient:
    """FastAPI dependency returning the active Connect client.

    Tests override this dependency with a fake, which is why the routers depend
    on it rather than constructing a client inline.
    """
    if settings.STRIPE_CONNECT_ENABLED and settings.STRIPE_SECRET_KEY:
        return StripeConnectClient()
    return SimulatedConnectClient()


# ---------------------------------------------------------------------------
# Account lifecycle
# ---------------------------------------------------------------------------
def _apply_account_payload(account: ConnectAccount, payload: dict[str, Any]) -> ConnectAccount:
    """Copy Stripe's capability verdict onto our row (Stripe is authoritative)."""
    account.payouts_enabled = bool(payload.get("payouts_enabled"))
    account.details_submitted = bool(payload.get("details_submitted"))
    requirements = payload.get("requirements")
    account.requirements = requirements if isinstance(requirements, dict) else None
    if account.payouts_enabled and account.details_submitted:
        account.status = "active"
    elif payload.get("disabled_reason") or payload.get("charges_enabled") is False:
        account.status = "restricted"
    else:
        account.status = "pending"
    account.updated_at = utcnow()
    return account


async def get_connect_account(db: AsyncSession, provider_id: uuid.UUID) -> ConnectAccount | None:
    return (
        await db.execute(select(ConnectAccount).where(ConnectAccount.provider_id == provider_id))
    ).scalar_one_or_none()


async def ensure_connect_account(
    db: AsyncSession,
    *,
    provider: Provider,
    email: str | None,
    client: ConnectClient,
) -> tuple[ConnectAccount, bool]:
    """Return the provider's Connect account, creating it at most once.

    Idempotent by the UNIQUE(provider_id) constraint AND by this lookup: calling
    onboarding twice must never leave a provider with two payout destinations.
    """
    existing = await get_connect_account(db, provider.id)
    if existing is not None:
        return existing, False

    payload = client.create_account(provider_id=str(provider.id), email=email)
    account_id = payload.get("id")
    if not account_id:
        raise ConnectError("connect_account_create_failed", status_code=502)
    account = ConnectAccount(provider_id=provider.id, stripe_account_id=str(account_id))
    _apply_account_payload(account, payload)
    db.add(account)
    await db.flush()
    return account, True


async def create_onboarding_link(
    *, account: ConnectAccount, client: ConnectClient, refresh_url: str, return_url: str
) -> dict[str, Any]:
    link = client.create_account_link(
        account_id=account.stripe_account_id, refresh_url=refresh_url, return_url=return_url
    )
    url = link.get("url")
    if not url:
        raise ConnectError("connect_account_link_failed", status_code=502)
    return {"url": url, "expires_at": link.get("expires_at")}


async def refresh_account_status(
    db: AsyncSession, *, account: ConnectAccount, client: ConnectClient
) -> ConnectAccount:
    payload = client.retrieve_account(account_id=account.stripe_account_id)
    _apply_account_payload(account, payload)
    await db.flush()
    return account


# ---------------------------------------------------------------------------
# Payouts
# ---------------------------------------------------------------------------
async def available_earnings_minor(db: AsyncSession, provider_id: uuid.UUID) -> int:
    account = await ledger.get_provider_account(db, provider_id)
    return int(account.balance_minor)


async def _payout_for_transaction(
    db: AsyncSession, txn: LedgerTransaction
) -> ProviderPayout | None:
    if not txn.reference_id:
        return None
    try:
        payout_id = uuid.UUID(txn.reference_id)
    except (ValueError, TypeError):
        return None
    return (
        await db.execute(select(ProviderPayout).where(ProviderPayout.id == payout_id))
    ).scalar_one_or_none()


async def create_connect_payout(
    db: AsyncSession,
    *,
    provider: Provider,
    amount_minor: int,
    idempotency_key: str,
    client: ConnectClient,
    actor_user_id: uuid.UUID | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    """Pay accrued earnings out over Stripe Connect. Idempotent on ``idempotency_key``.

    Ledger shape (two balanced postings, both keyed off the payout row):

    1. ``connect_payout_request`` — debit ``provider_earnings`` / credit
       ``payout_pending``: the earnings become a platform liability.
    2. ``connect_payout_settle`` — debit ``payout_pending`` / credit ``external``:
       the money leaves the platform once Stripe accepted the transfer.
    """
    if amount_minor <= 0:
        raise ConnectError("amount_must_be_positive", status_code=422)

    account = await get_connect_account(db, provider.id)
    if account is None:
        raise ConnectError("connect_account_missing", status_code=409)
    # KYC gate — Stripe's verdict, not ours.
    if not (account.payouts_enabled and account.details_submitted and account.status == _PAYABLE_STATUS):
        raise ConnectError("connect_payouts_not_enabled", status_code=409)

    request_key = f"connect_payout:{provider.id}:{idempotency_key}"

    # Replay: the same key already produced a payout -> return it, pay nothing.
    prior = (
        await db.execute(
            select(LedgerTransaction).where(LedgerTransaction.idempotency_key == request_key)
        )
    ).scalar_one_or_none()
    if prior is not None:
        payout = await _payout_for_transaction(db, prior)
        if payout is not None:
            return _shape_payout_result(payout, account, client, replayed=True)

    earnings = await ledger.get_provider_account(db, provider.id)
    if amount_minor > int(earnings.balance_minor):
        raise ConnectError("insufficient_earnings", status_code=409)

    payout_pending = await ledger.get_platform_account(db, "payout_pending")
    external = await ledger.get_platform_account(db, "external")

    payout = ProviderPayout(
        provider_id=provider.id,
        amount_minor=amount_minor,
        status="requested",
        actor_user_id=actor_user_id,
    )
    db.add(payout)
    await db.flush()

    try:
        request_txn = await ledger.post_transaction(
            db,
            kind="connect_payout_request",
            legs=[(earnings, "debit", amount_minor), (payout_pending, "credit", amount_minor)],
            idempotency_key=request_key,
            reference_type="connect_payout",
            reference_id=str(payout.id),
            actor_user_id=actor_user_id,
        )
    except ledger.InsufficientFunds:
        # The pre-check above is advisory; THIS is the authoritative refusal.
        await db.delete(payout)
        await db.flush()
        raise ConnectError("insufficient_earnings", status_code=409)

    if request_txn.reference_id != str(payout.id):
        # Lost a race on the same idempotency key: the first request won. Drop
        # our row and report the winner — exactly one payout per key.
        await db.delete(payout)
        await db.flush()
        winner = await _payout_for_transaction(db, request_txn)
        if winner is not None:
            return _shape_payout_result(winner, account, client, replayed=True)
        raise ConnectError("payout_conflict", status_code=409)

    payout.request_txn_id = request_txn.id

    transfer = client.create_transfer(
        amount_minor=amount_minor,
        currency=earnings.currency.lower() if earnings.currency else "usd",
        destination=account.stripe_account_id,
        idempotency_key=request_key,
        metadata={"payout_id": str(payout.id), "provider_id": str(provider.id)},
    )
    transfer_id = str(transfer.get("id") or "")

    settle_txn = await ledger.post_transaction(
        db,
        kind="connect_payout_settle",
        legs=[(payout_pending, "debit", amount_minor), (external, "credit", amount_minor)],
        idempotency_key=f"connect_payout_settle:{payout.id}",
        reference_type="connect_payout",
        reference_id=str(payout.id),
        actor_user_id=actor_user_id,
    )
    payout.settle_txn_id = settle_txn.id
    payout.status = "settled"
    payout.settled_at = utcnow()

    await record_audit(
        db,
        action="provider.connect.payout",
        outcome="success",
        actor_user_id=actor_user_id,
        ip=ip,
        resource_type="payout",
        resource_id=str(payout.id),
        reason=f"{getattr(client, 'mode', 'simulated')}:{transfer_id}:{ledger.minor_to_str(amount_minor)}",
    )
    await db.flush()

    result = _shape_payout_result(payout, account, client, replayed=False)
    result["transfer_id"] = transfer_id
    return result


def _shape_payout_result(
    payout: ProviderPayout,
    account: ConnectAccount,
    client: ConnectClient,
    *,
    replayed: bool,
) -> dict[str, Any]:
    return {
        "payout_id": payout.id,
        "amount": ledger.minor_to_str(payout.amount_minor),
        "currency": payout.currency,
        "status": payout.status,
        "destination_account": account.stripe_account_id,
        "mode": getattr(client, "mode", "simulated"),
        "replayed": replayed,
        "created_at": payout.created_at,
        "settled_at": payout.settled_at,
        "transfer_id": None,
    }


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
async def _reverse_failed_payout(
    db: AsyncSession, payout: ProviderPayout, *, reason: str
) -> None:
    """Return a failed transfer's money to the provider's earnings.

    Net effect of the reversal: ``external`` debited, ``provider_earnings``
    credited — the mirror image of request+settle (``payout_pending`` nets to
    zero, so it is left out of the posting rather than double-touched).
    """
    if payout.status == "cancelled":
        return
    earnings = await ledger.get_provider_account(db, payout.provider_id)
    external = await ledger.get_platform_account(db, "external")
    await ledger.post_transaction(
        db,
        kind="connect_payout_reversal",
        legs=[(external, "debit", payout.amount_minor), (earnings, "credit", payout.amount_minor)],
        idempotency_key=f"connect_payout_reversal:{payout.id}",
        reference_type="connect_payout",
        reference_id=str(payout.id),
    )
    payout.status = "cancelled"
    payout.settled_at = None
    await record_audit(
        db,
        action="provider.connect.payout.failed",
        outcome="reversed",
        resource_type="payout",
        resource_id=str(payout.id),
        reason=reason[:500],
    )
    await db.flush()


async def handle_connect_webhook(
    db: AsyncSession,
    *,
    payload: bytes,
    signature_header: str | None,
    secret: str | None = None,
    construct_event_fn=None,
) -> dict[str, Any]:
    """Verify and apply a Connect webhook event.

    A forged or unsigned event raises :class:`StripeSignatureError` BEFORE any
    state is touched. Every handled effect is idempotent, because Stripe retries
    deliveries and may deliver the same event twice.
    """
    verify = construct_event_fn or verify_stripe_signature
    webhook_secret = secret or settings.STRIPE_WEBHOOK_SECRET
    if not signature_header:
        raise StripeSignatureError("Missing Stripe signature")
    event = verify(payload, signature_header, webhook_secret)

    event_id = event.get("id") if isinstance(event, dict) else event.id
    event_type = event.get("type") if isinstance(event, dict) else event.type
    data_object = (
        event.get("data", {}).get("object", {}) if isinstance(event, dict) else event.data.object
    )
    if hasattr(data_object, "to_dict"):
        data_object = dict(data_object.to_dict())
    if not isinstance(data_object, dict):
        data_object = {}

    if event_type == "account.updated":
        account_id = data_object.get("id")
        account = (
            await db.execute(
                select(ConnectAccount).where(ConnectAccount.stripe_account_id == str(account_id))
            )
        ).scalar_one_or_none()
        if account is None:
            return {"received": True, "event_id": event_id, "status": "unknown_account"}
        _apply_account_payload(account, data_object)
        await db.flush()
        return {
            "received": True,
            "event_id": event_id,
            "status": "account_updated",
            "payouts_enabled": account.payouts_enabled,
            "account_status": account.status,
        }

    if event_type in ("transfer.failed", "payout.failed", "transfer.reversed"):
        payout_id = (data_object.get("metadata") or {}).get("payout_id")
        if not payout_id:
            return {"received": True, "event_id": event_id, "status": "no_payout_reference"}
        try:
            payout_uuid = uuid.UUID(str(payout_id))
        except (ValueError, TypeError):
            return {"received": True, "event_id": event_id, "status": "bad_payout_reference"}
        payout = (
            await db.execute(select(ProviderPayout).where(ProviderPayout.id == payout_uuid))
        ).scalar_one_or_none()
        if payout is None:
            return {"received": True, "event_id": event_id, "status": "unknown_payout"}
        await _reverse_failed_payout(db, payout, reason=f"{event_type}:{event_id}")
        return {"received": True, "event_id": event_id, "status": "payout_reversed"}

    if event_type in ("transfer.created", "transfer.paid", "payout.paid"):
        # Money already recorded at creation time; this is a confirmation only.
        return {"received": True, "event_id": event_id, "status": "acknowledged"}

    return {"received": True, "event_id": event_id, "status": "unhandled_event"}
