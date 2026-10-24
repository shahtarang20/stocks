"""Stripe Connect onboarding + payouts for providers (Phase C3).

All money movement happens in :mod:`app.services.connect_payouts` (which in turn
only moves money through the ledger). This router is transport: authn/authz,
input validation, and mapping refusals onto stable HTTP codes.

Security notes:
* onboarding/payout require a VERIFIED user — paying out to an unverified
  account is the classic account-takeover cash-out path;
* the Stripe client is an injected dependency, so tests never touch the network;
* ``/webhook`` is unauthenticated by necessity and therefore signature-verified:
  a forged payload is rejected with 400 before anything is read from it.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.deps import client_ip, get_current_user, require_verified_user
from app.models.enterprise import ConnectAccount
from app.schemas.billing import parse_money
from app.schemas.auth import CurrentUser
from app.services import ledger
from app.services.audit import record_audit
from app.services.connect_payouts import (
    ConnectClient,
    ConnectError,
    available_earnings_minor,
    create_connect_payout,
    create_onboarding_link,
    ensure_connect_account,
    get_connect_account,
    get_connect_client,
    handle_connect_webhook,
    refresh_account_status,
)
from app.services.provider import get_provider_by_user
from app.services.stripe_payment import StripeSignatureError

router = APIRouter(prefix="/v1/provider/connect", tags=["provider-connect"])

_NOT_A_PROVIDER = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN, detail="Not registered as a provider"
)
_ACCOUNT_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="connect_account_missing"
)


def _safe_url(raw: str | None, fallback: str) -> str:
    """Accept only absolute http(s) URLs for Stripe redirect targets.

    Stripe will send the operator's browser here; a ``javascript:`` or ``data:``
    target would turn onboarding into an XSS vector.
    """
    if not raw:
        return fallback
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_redirect_url"
        )
    return raw


class OnboardingRequest(BaseModel):
    refresh_url: str | None = Field(default=None, max_length=2048)
    return_url: str | None = Field(default=None, max_length=2048)


class ConnectAccountOut(BaseModel):
    account_id: uuid.UUID
    stripe_account_id: str
    status: str
    payouts_enabled: bool
    details_submitted: bool
    requirements: dict | None = None
    available_earnings: str
    mode: str
    created_at: dt.datetime


class OnboardingOut(ConnectAccountOut):
    onboarding_url: str
    onboarding_expires_at: int | None = None
    created: bool


class ConnectPayoutRequest(BaseModel):
    amount: decimal.Decimal = Field(gt=0)
    # Caller-supplied stable key: a retried request must pay exactly once.
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce(cls, v):
        return parse_money(v)


class ConnectPayoutOut(BaseModel):
    payout_id: uuid.UUID
    amount: str
    currency: str
    status: str
    destination_account: str
    mode: str
    replayed: bool
    transfer_id: str | None = None
    created_at: dt.datetime
    settled_at: dt.datetime | None = None


async def _require_provider(db: AsyncSession, current: CurrentUser):
    provider = await get_provider_by_user(db, current.id)
    if provider is None:
        raise _NOT_A_PROVIDER
    return provider


def _shape_account(
    account: ConnectAccount, *, available_minor: int, mode: str
) -> dict:
    return {
        "account_id": account.id,
        "stripe_account_id": account.stripe_account_id,
        "status": account.status,
        "payouts_enabled": account.payouts_enabled,
        "details_submitted": account.details_submitted,
        "requirements": account.requirements,
        "available_earnings": ledger.minor_to_str(available_minor),
        "mode": mode,
        "created_at": account.created_at,
    }


@router.post("/onboarding", response_model=OnboardingOut, status_code=status.HTTP_201_CREATED)
async def start_onboarding(
    body: OnboardingRequest,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
    client: ConnectClient = Depends(get_connect_client),
) -> OnboardingOut:
    """Create (once) the provider's Connect account and return an onboarding link."""
    provider = await _require_provider(db, current)
    try:
        account, created = await ensure_connect_account(
            db, provider=provider, email=current.email, client=client
        )
        link = await create_onboarding_link(
            account=account,
            client=client,
            refresh_url=_safe_url(body.refresh_url, settings.STRIPE_CANCEL_URL_DEFAULT),
            return_url=_safe_url(body.return_url, settings.STRIPE_SUCCESS_URL_DEFAULT),
        )
    except ConnectError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc

    if created:
        await record_audit(
            db,
            action="provider.connect.onboarding",
            outcome="success",
            actor_user_id=current.id,
            ip=client_ip(request),
            resource_type="connect_account",
            resource_id=str(account.id),
            reason=getattr(client, "mode", "simulated"),
        )
    available = await available_earnings_minor(db, provider.id)
    return OnboardingOut(
        **_shape_account(account, available_minor=available, mode=getattr(client, "mode", "simulated")),
        onboarding_url=link["url"],
        onboarding_expires_at=link.get("expires_at"),
        created=created,
    )


@router.get("/account", response_model=ConnectAccountOut)
async def read_account(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    client: ConnectClient = Depends(get_connect_client),
) -> ConnectAccountOut:
    provider = await _require_provider(db, current)
    account = await get_connect_account(db, provider.id)
    if account is None:
        raise _ACCOUNT_NOT_FOUND
    available = await available_earnings_minor(db, provider.id)
    return ConnectAccountOut(
        **_shape_account(account, available_minor=available, mode=getattr(client, "mode", "simulated"))
    )


@router.post("/account/refresh", response_model=ConnectAccountOut)
async def refresh_account(
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
    client: ConnectClient = Depends(get_connect_client),
) -> ConnectAccountOut:
    """Re-read Stripe's KYC verdict. Stripe is authoritative; we never self-approve."""
    provider = await _require_provider(db, current)
    account = await get_connect_account(db, provider.id)
    if account is None:
        raise _ACCOUNT_NOT_FOUND
    try:
        account = await refresh_account_status(db, account=account, client=client)
    except ConnectError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    available = await available_earnings_minor(db, provider.id)
    return ConnectAccountOut(
        **_shape_account(account, available_minor=available, mode=getattr(client, "mode", "simulated"))
    )


@router.post("/payouts", response_model=ConnectPayoutOut, status_code=status.HTTP_201_CREATED)
async def create_payout(
    body: ConnectPayoutRequest,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
    client: ConnectClient = Depends(get_connect_client),
) -> ConnectPayoutOut:
    """Pay accrued earnings out over Connect. Idempotent on ``idempotency_key``."""
    provider = await _require_provider(db, current)
    try:
        result = await create_connect_payout(
            db,
            provider=provider,
            amount_minor=ledger.to_minor(body.amount),
            idempotency_key=body.idempotency_key,
            client=client,
            actor_user_id=current.id,
            ip=client_ip(request),
        )
    except ConnectError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    return ConnectPayoutOut(**result)


@router.post("/webhook")
async def connect_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Signature-verified Connect events (account.updated, transfer/payout results)."""
    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    try:
        return await handle_connect_webhook(
            db, payload=payload, signature_header=signature
        )
    except StripeSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.code
        ) from exc
