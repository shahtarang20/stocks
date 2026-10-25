"""Wallet linking and the read-only on-chain COMPUTE balance.

GET    /v1/wallet                 credits + linked wallet + on-chain token balance
POST   /v1/wallet/link-challenge  the message the user's wallet must sign
POST   /v1/wallet/link            verify the signature and link the address
DELETE /v1/wallet/link            unlink

The ledger (credits) and the chain (tokens) are shown side by side but are different
things: nothing here moves value between them.
"""

from __future__ import annotations

import decimal
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rate_limit import rate_limiter
from app.db.base import utcnow
from app.db.session import get_db
from app.deps import client_ip, get_current_user, require_roles, require_verified_user
from app.models.token_deposit import TokenDeposit
from app.models.wallet import LinkedWallet
from app.schemas.auth import CurrentUser
from app.services import billing as billing_svc
from app.services import ledger
from app.services import token_deposits as deposits_svc
from app.services import wallet as wallet_svc
from app.services.audit import record_audit
from app.services.chain_reconciler import ChainClientError, JsonRpcChainClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/wallet", tags=["wallet"])

TOKEN_SYMBOL = "COMPUTE"
TOKEN_DECIMALS = 18


class LinkChallengeRequest(BaseModel):
    address: str = Field(min_length=1, max_length=64)
    chain_id: int | None = None


class LinkRequest(LinkChallengeRequest):
    message: str = Field(min_length=1, max_length=2000)
    signature: str = Field(min_length=1, max_length=400)


def _rate_limit(user_id) -> None:
    if not rate_limiter.allow(f"wallet_link:{user_id}", 20, 3600):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests, please try again later")


def _chain_id(requested: int | None) -> int:
    configured = settings.wallet_chain_id
    if requested is not None and requested != configured:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "wrong_chain")
    return configured


def _fmt_tokens(raw: int) -> str:
    value = decimal.Decimal(raw) / (decimal.Decimal(10) ** TOKEN_DECIMALS)
    return f"{value:.6f}"


def _chain_info() -> dict:
    return {
        "chain_id": settings.wallet_chain_id,
        "chain_name": settings.WALLET_CHAIN_NAME or None,
        "token_address": settings.COMPUTE_TOKEN_ADDRESS,
        "symbol": TOKEN_SYMBOL,
        "decimals": TOKEN_DECIMALS,
        "explorer_base": settings.WALLET_EXPLORER_BASE or None,
        "configured": bool(settings.CHAIN_RPC_URL.strip()),
    }


def _deposit_info() -> dict:
    if not settings.token_deposits_enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "address": settings.TOKEN_DEPOSIT_ADDRESS,
        "credits_per_token": settings.TOKEN_CREDITS_PER_TOKEN,
        "confirmations": settings.TOKEN_DEPOSIT_CONFIRMATIONS,
        "min_tokens": settings.TOKEN_DEPOSIT_MIN_TOKENS,
        "max_tokens": settings.TOKEN_DEPOSIT_MAX_TOKENS,
    }


def _deposit_out(d: TokenDeposit) -> dict:
    return {
        "tx_hash": d.tx_hash,
        "block_number": d.block_number,
        "amount": f"{deposits_svc.tokens_from_raw(int(d.amount_raw)):.6f}",
        "status": d.status,
        "reason": d.reason,
        "credits": ledger.minor_to_str(d.credits_minor) if d.credits_minor is not None else None,
        "created_at": d.created_at,
    }


@router.get("")
async def get_wallet(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    account = await ledger.get_or_create_customer_account(db, current.id)
    reserved = await billing_svc.customer_reserved_minor(db, current.id)
    row = (
        await db.execute(
            select(LinkedWallet).where(
                LinkedWallet.user_id == current.id, LinkedWallet.chain_id == settings.wallet_chain_id
            )
        )
    ).scalar_one_or_none()
    await db.commit()

    out: dict = {
        "credits": {
            "currency": account.currency,
            "available": ledger.minor_to_str(account.balance_minor),
            "reserved": ledger.minor_to_str(reserved),
            "balance": ledger.minor_to_str(account.balance_minor + reserved),
            "simulated": True,
        },
        "chain": _chain_info(),
        "wallet": None,
        "token_balance": None,
        "token_balance_error": None,
        "deposits": _deposit_info(),
    }
    if row is None:
        return out
    out["wallet"] = {
        "address": wallet_svc.display_address(row.address),
        "chain_id": row.chain_id,
        "linked_at": row.linked_at,
    }
    if out["chain"]["configured"]:
        try:
            raw = await JsonRpcChainClient().token_balance(settings.COMPUTE_TOKEN_ADDRESS, row.address)
            out["token_balance"] = _fmt_tokens(raw)
        except ChainClientError as exc:
            logger.warning("wallet balance lookup failed: %s", exc)
            out["token_balance_error"] = "chain_unavailable"
    return out


@router.post("/link-challenge")
async def link_challenge(
    body: LinkChallengeRequest,
    current: CurrentUser = Depends(require_verified_user),
) -> dict:
    _rate_limit(current.id)
    chain_id = _chain_id(body.chain_id)
    try:
        address = wallet_svc.normalise_address(body.address)
    except wallet_svc.WalletLinkError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    return {
        "message": wallet_svc.build_message(current.id, address, chain_id, utcnow()),
        "chain_id": chain_id,
        "expires_in_seconds": settings.WALLET_LINK_MESSAGE_TTL_SECONDS,
    }


@router.post("/link", status_code=status.HTTP_201_CREATED)
async def link_wallet(
    body: LinkRequest,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _rate_limit(current.id)
    chain_id = _chain_id(body.chain_id)
    try:
        address = wallet_svc.verify_link(
            user_id=current.id,
            address=body.address,
            chain_id=chain_id,
            message=body.message,
            signature=body.signature,
            now=utcnow(),
        )
    except wallet_svc.WalletLinkError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    owner = (
        await db.execute(
            select(LinkedWallet).where(LinkedWallet.chain_id == chain_id, LinkedWallet.address == address)
        )
    ).scalar_one_or_none()
    if owner is not None:
        if owner.user_id == current.id:
            return {"address": wallet_svc.display_address(address), "chain_id": chain_id}  # idempotent
        raise HTTPException(status.HTTP_409_CONFLICT, "address_linked_to_another_account")
    existing = (
        await db.execute(
            select(LinkedWallet).where(LinkedWallet.user_id == current.id, LinkedWallet.chain_id == chain_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "account_already_has_a_wallet")

    db.add(LinkedWallet(user_id=current.id, chain_id=chain_id, address=address))
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "address_linked_to_another_account") from None
    await record_audit(
        db, action="wallet.link", outcome="success", actor_user_id=current.id,
        ip=client_ip(request), resource_type="wallet", resource_id=address,
    )
    return {"address": wallet_svc.display_address(address), "chain_id": chain_id}


@router.delete("/link", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def unlink_wallet(
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    row = (
        await db.execute(
            select(LinkedWallet).where(
                LinkedWallet.user_id == current.id, LinkedWallet.chain_id == settings.wallet_chain_id
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await record_audit(
            db, action="wallet.unlink", outcome="success", actor_user_id=current.id,
            ip=client_ip(request), resource_type="wallet", resource_id=row.address,
        )
        await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/deposits")
async def my_deposits(
    limit: int = Query(50, ge=1, le=200),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """The caller's own COMPUTE deposits, newest first."""
    rows = (
        await db.execute(
            select(TokenDeposit)
            .where(TokenDeposit.user_id == current.id)
            .order_by(TokenDeposit.created_at.desc(), TokenDeposit.block_number.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [_deposit_out(d) for d in rows]


admin_router = APIRouter(
    prefix="/v1/admin/token-deposits",
    tags=["wallet-admin"],
    dependencies=[Depends(require_roles("administrator"))],
)


@admin_router.get("")
async def admin_list_deposits(
    status_filter: str | None = Query(None, alias="status", pattern="^(credited|unclaimed|rejected)$"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    stmt = select(TokenDeposit).order_by(TokenDeposit.created_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(TokenDeposit.status == status_filter)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {**_deposit_out(d), "user_id": str(d.user_id) if d.user_id else None, "from_address": d.from_address}
        for d in rows
    ]


@admin_router.post("/scan")
async def admin_scan(db: AsyncSession = Depends(get_db)) -> dict:
    """Run a deposit scan now (the worker also runs one every sweep)."""
    if not settings.token_deposits_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "token_deposits_disabled")
    try:
        return await deposits_svc.scan_deposits(db)
    except ChainClientError as exc:
        await db.rollback()
        logger.warning("deposit scan failed: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "chain_unavailable") from None


@admin_router.get("/reconciliation")
async def admin_reconciliation(db: AsyncSession = Depends(get_db)) -> dict:
    """Does the chain agree with the ledger? ``ok`` is false if either side disagrees."""
    if not settings.token_deposits_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "token_deposits_disabled")
    try:
        return await deposits_svc.reconcile(db)
    except ChainClientError as exc:
        logger.warning("deposit reconciliation failed: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "chain_unavailable") from None
