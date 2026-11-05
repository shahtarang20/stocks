"""Token withdrawals (B1, ADR-0004 stage 2). MONEY CODE: needs owner approval before it is enabled.

The invariant: credits and tokens never both exist for the same money. A withdrawal moves the credits
OUT of the customer's account FIRST (into ``withdrawal_pending``), in one balanced ledger transaction;
only then can the worker send tokens. The money ends in exactly one place:

    request :  customer_credits  -> withdrawal_pending      (kind payout_request)
    success :  withdrawal_pending -> paid_out                (kind payout_settle)
    failure :  withdrawal_pending -> customer_credits        (kind refund, with refund_reason)

Safety rules, each tested:
* off unless WITHDRAWALS_ENABLED and fully configured (``settings.withdrawals_configured``), and not paused
* destination must be the user's own signature-verified LinkedWallet on the configured chain
* idempotency key required; a repeat returns the same withdrawal; the same key with other values is refused
* minimum, per-transaction, per-user daily and global daily caps (all from settings)
* one in-flight transaction per hot wallet (worker lock + unique partial index)
* the signed transaction and its hash are stored BEFORE broadcast, so a crash re-broadcasts the same tx
* a timeout is refunded only when the nonce is provably used by another transaction; otherwise the
  system pauses and an admin decides (refunding a transaction that might still mine would pay twice)
"""

from __future__ import annotations

import datetime as dt
import decimal
import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.billing import BillingAccount
from app.models.wallet import LinkedWallet
from app.models.withdrawal import IN_FLIGHT_STATUSES, OPEN_STATUSES, Withdrawal
from app.services import ledger
from app.services.audit import record_audit
from app.services.chain_reconciler import ChainClientError
from app.services.withdrawal_chain import JsonRpcPayoutChain, PayoutChain, transfer_calldata, tx_hash_of
from app.services.withdrawal_pause import is_paused, set_pause
from app.services.withdrawal_signer import NotConfigured, SignerError, WithdrawalSigner, build_default_signer

logger = logging.getLogger(__name__)

TOKEN_DECIMALS = 18
_UNIT = 10**TOKEN_DECIMALS


class WithdrawalError(Exception):
    """A refusal with a stable code and the HTTP status the router should return."""

    def __init__(self, code: str, http_status: int = 422):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _credits(value: str) -> int:
    return ledger.to_minor(decimal.Decimal(value))


def raw_tokens_for_credits(credits_minor: int) -> int:
    """Tokens (smallest unit) for an amount of credits at the deposit rate, rounded DOWN (never overpay)."""
    rate = decimal.Decimal(settings.TOKEN_CREDITS_PER_TOKEN)
    tokens = (decimal.Decimal(int(credits_minor)) / 100) / rate
    return int((tokens * _UNIT).to_integral_value(rounding=decimal.ROUND_FLOOR))


async def _platform_account(db: AsyncSession, account_type: str) -> BillingAccount:
    return await ledger.get_platform_account(db, account_type)


async def _window_sum(db: AsyncSession, user_id: uuid.UUID | None) -> int:
    since = utcnow() - dt.timedelta(hours=24)
    stmt = select(func.coalesce(func.sum(Withdrawal.credits_minor), 0)).where(
        Withdrawal.status != "refunded", Withdrawal.created_at >= since
    )
    if user_id is not None:
        stmt = stmt.where(Withdrawal.user_id == user_id)
    return int((await db.execute(stmt)).scalar_one())


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
async def request_withdrawal(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    amount: decimal.Decimal,
    destination: str,
    idempotency_key: str,
    ip: str | None = None,
) -> tuple[Withdrawal, bool]:
    """Create a withdrawal and hold the credits. Returns (withdrawal, created). The caller commits."""
    if not settings.withdrawals_configured:
        raise WithdrawalError("withdrawals_disabled", 503)
    if await is_paused(db):
        raise WithdrawalError("withdrawals_paused", 503)
    key = (idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise WithdrawalError("idempotency_key_required", 422)

    chain_id = settings.wallet_chain_id
    dest = (destination or "").strip().lower()
    credits = ledger.to_minor(amount)

    # Serialize this customer's money moves, then handle a repeat before any other check.
    await ledger.lock_customer(db, user_id)
    existing = (
        await db.execute(select(Withdrawal).where(Withdrawal.user_id == user_id, Withdrawal.idempotency_key == key))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.destination != dest or existing.credits_minor != credits:
            raise WithdrawalError("idempotency_key_reused", 409)
        return existing, False

    # The destination must be the user's own linked wallet on THIS chain. Nothing else is ever paid.
    wallet = (
        await db.execute(
            select(LinkedWallet).where(LinkedWallet.user_id == user_id, LinkedWallet.chain_id == chain_id)
        )
    ).scalar_one_or_none()
    if wallet is None:
        raise WithdrawalError("wallet_not_linked", 409)
    if wallet.address.lower() != dest:
        raise WithdrawalError("destination_not_linked_wallet", 422)

    if credits < _credits(settings.WITHDRAWALS_MIN_CREDITS):
        raise WithdrawalError("below_minimum", 422)
    if credits > _credits(settings.WITHDRAWALS_MAX_PER_TX_CREDITS):
        raise WithdrawalError("above_per_transaction_cap", 422)

    # Daily caps. The global check needs one lock so two requests cannot both squeeze under it.
    await ledger._advisory_xact_lock(db, "withdrawal_caps")
    if await _window_sum(db, user_id) + credits > _credits(settings.WITHDRAWALS_DAILY_USER_CREDITS):
        raise WithdrawalError("daily_user_cap_exceeded", 429)
    if await _window_sum(db, None) + credits > _credits(settings.WITHDRAWALS_DAILY_GLOBAL_CREDITS):
        raise WithdrawalError("daily_global_cap_exceeded", 429)

    raw = raw_tokens_for_credits(credits)
    if raw <= 0:
        raise WithdrawalError("amount_too_small", 422)

    wd = Withdrawal(
        user_id=user_id,
        idempotency_key=key,
        chain_id=chain_id,
        token_address=settings.COMPUTE_TOKEN_ADDRESS.lower(),
        destination=dest,
        credits_minor=credits,
        amount_raw=str(raw),
        status="pending",
    )
    try:
        async with db.begin_nested():
            db.add(wd)
            await db.flush()
            customer = await ledger.get_or_create_customer_account(db, user_id)
            pending = await _platform_account(db, "withdrawal_pending")
            try:
                txn = await ledger.post_transaction(
                    db,
                    kind="payout_request",
                    legs=[(customer, "debit", credits), (pending, "credit", credits)],
                    idempotency_key=f"withdrawal_request:{wd.id}",
                    reference_type="withdrawal",
                    reference_id=str(wd.id),
                    actor_user_id=user_id,
                )
            except ledger.InsufficientFunds:
                raise WithdrawalError("insufficient_funds", 402) from None
            wd.request_txn_id = txn.id
            await db.flush()
    except IntegrityError:
        # A parallel request with the same key won the unique constraint: return its row.
        again = (
            await db.execute(
                select(Withdrawal).where(Withdrawal.user_id == user_id, Withdrawal.idempotency_key == key)
            )
        ).scalar_one_or_none()
        if again is None:
            raise
        return again, False
    await record_audit(
        db, action="withdrawal.request", outcome="success", actor_user_id=user_id,
        resource_type="withdrawal", resource_id=str(wd.id), ip=ip,
    )
    return wd, True


# ---------------------------------------------------------------------------
# Final states (each is one balanced ledger transaction plus an audit record)
# ---------------------------------------------------------------------------
async def _mark_paid_out(db: AsyncSession, wd: Withdrawal, receipt_block: int) -> None:
    pending = await _platform_account(db, "withdrawal_pending")
    paid = await _platform_account(db, "paid_out")
    txn = await ledger.post_transaction(
        db,
        kind="payout_settle",
        legs=[(pending, "debit", wd.credits_minor), (paid, "credit", wd.credits_minor)],
        idempotency_key=f"withdrawal_paid:{wd.id}",
        reference_type="withdrawal",
        reference_id=str(wd.id),
    )
    wd.status, wd.receipt_block, wd.final_txn_id = "paid_out", receipt_block, txn.id
    await db.flush()
    await record_audit(
        db, action="withdrawal.paid_out", outcome="success", resource_type="withdrawal", resource_id=str(wd.id)
    )


async def refund_withdrawal(db: AsyncSession, wd: Withdrawal, reason: str, actor_user_id: uuid.UUID | None = None) -> None:
    """Return the held credits to the user and record why. Only an open withdrawal can be refunded."""
    if wd.status not in OPEN_STATUSES:
        raise WithdrawalError("not_refundable", 409)
    pending = await _platform_account(db, "withdrawal_pending")
    customer = await ledger.get_or_create_customer_account(db, wd.user_id)
    txn = await ledger.post_transaction(
        db,
        kind="refund",
        legs=[(pending, "debit", wd.credits_minor), (customer, "credit", wd.credits_minor)],
        idempotency_key=f"withdrawal_refund:{wd.id}",
        reference_type="withdrawal",
        reference_id=str(wd.id),
        actor_user_id=actor_user_id,
    )
    wd.status, wd.refund_reason, wd.final_txn_id = "refunded", reason[:64], txn.id
    await db.flush()
    await record_audit(
        db, action="withdrawal.refunded", outcome="success", actor_user_id=actor_user_id,
        resource_type="withdrawal", resource_id=str(wd.id), reason=reason,
    )


async def cancel_pending(db: AsyncSession, wd: Withdrawal, actor_user_id: uuid.UUID | None, reason: str = "cancelled") -> None:
    """Refund a withdrawal that was NEVER signed. Anything already signed is resolved by the worker."""
    if wd.status != "pending" or wd.tx_hash is not None:
        raise WithdrawalError("not_cancellable", 409)
    await refund_withdrawal(db, wd, reason, actor_user_id)


# ---------------------------------------------------------------------------
# Payout worker
# ---------------------------------------------------------------------------
_ALREADY_KNOWN_MARKERS = ("already known", "known transaction", "nonce too low", "already imported", "already exists")


def _node_already_has_tx(message: str) -> bool:
    """True when an RPC error means "this exact signed transaction is already in the pool or mined"."""
    low = message.lower()
    return any(m in low for m in _ALREADY_KNOWN_MARKERS)


async def _advance_in_flight(db: AsyncSession, chain: PayoutChain, out: dict[str, Any]) -> bool:
    """Move the (at most one) in-flight withdrawal forward. Returns True while one is still in flight."""
    wd = (
        await db.execute(
            select(Withdrawal).where(Withdrawal.status.in_(IN_FLIGHT_STATUSES)).order_by(Withdrawal.created_at).limit(1).with_for_update()
        )
    ).scalar_one_or_none()
    if wd is None:
        return False

    if wd.status == "sending":
        # Signed and stored before broadcast; (re)send the SAME transaction. Safe to repeat.
        try:
            await chain.send_raw(wd.raw_tx)
        except ChainClientError as exc:
            # A crash between the broadcast and the status update leaves "sending". The retry is then refused by the node
            # ("already known" while it is in the mempool, "nonce too low" once it is mined). That is not a failure: the
            # stored transaction hash is the truth. Without this the withdrawal stayed "sending" forever, the retry raised on
            # every sweep, and (one payout in flight at a time) every later withdrawal was blocked behind it.
            if not _node_already_has_tx(str(exc)):
                raise
            logger.info("withdrawal %s: node already has the stored transaction (%s); watching its receipt", wd.id, exc)
        wd.status, wd.broadcast_at = "broadcast", utcnow()
        await db.commit()
        out["broadcast"] = out.get("broadcast", 0) + 1
        # fall through: the receipt (if it is already mined) decides refund or paid-out in this same pass

    receipt = await chain.receipt(wd.tx_hash)
    if receipt is not None:
        if int(receipt["status"]) != 1:
            await refund_withdrawal(db, wd, "reverted")
            await db.commit()
            out["refunded"] = out.get("refunded", 0) + 1
            return False
        block = int(receipt["block_number"])
        wd.receipt_block = block
        latest = await chain.latest_block()
        if latest - block >= int(settings.WITHDRAWALS_CONFIRMATIONS):
            await _mark_paid_out(db, wd, block)
            await db.commit()
            out["paid_out"] = out.get("paid_out", 0) + 1
            return False
        await db.commit()
        return True

    # No receipt. A previously seen receipt that vanished means a reorg: wait for it to re-mine.
    wd.receipt_block = None
    started = wd.broadcast_at or wd.created_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=dt.timezone.utc)
    if (utcnow() - started).total_seconds() > int(settings.WITHDRAWALS_TIMEOUT_SECONDS):
        used = await chain.nonce(wd.hot_wallet, "latest")
        if wd.nonce is not None and used > wd.nonce:
            # The nonce is spent by some other transaction and ours has no receipt: it can never mine.
            await refund_withdrawal(db, wd, "timeout_nonce_used")
            await db.commit()
            out["refunded"] = out.get("refunded", 0) + 1
            return False
        # Nonce still free: our transaction might yet mine. Never refund (double pay). Pause, ask a human.
        await set_pause(db, True, reason=f"payout stuck: withdrawal {wd.id}", source="reconciliation")
        out["stuck"] = str(wd.id)
    await db.commit()
    return True


def _checksum(address: str) -> str:
    from eth_utils import to_checksum_address

    return to_checksum_address(address.strip())


async def _start_next(db: AsyncSession, chain: PayoutChain, signer: WithdrawalSigner, out: dict[str, Any]) -> None:
    wd = (
        await db.execute(
            select(Withdrawal).where(Withdrawal.status == "pending").order_by(Withdrawal.created_at).limit(1).with_for_update()
        )
    ).scalar_one_or_none()
    if wd is None:
        return
    hot = settings.WITHDRAWALS_HOT_WALLET_ADDRESS.strip().lower()
    if signer.address.lower() != hot:
        raise SignerError("signer_address_is_not_hot_wallet")
    amount = int(wd.amount_raw)
    if await chain.token_balance(settings.COMPUTE_TOKEN_ADDRESS, hot) < amount:
        out["waiting"] = "hot_wallet_balance"  # leave pending; top up the hot wallet
        return
    gas_price = await chain.gas_price()
    if gas_price > int(settings.WITHDRAWALS_MAX_GAS_PRICE_WEI):
        out["waiting"] = "gas_price_above_cap"
        return
    nonce = await chain.nonce(hot, "pending")
    raw_tx = await signer.sign_transaction(
        {
            "chainId": int(wd.chain_id),
            "nonce": nonce,
            "gasPrice": gas_price,
            "gas": int(settings.WITHDRAWALS_GAS_LIMIT),
            "to": _checksum(settings.COMPUTE_TOKEN_ADDRESS),  # eth-account refuses non-checksummed 'to'
            "value": 0,
            "data": transfer_calldata(wd.destination, amount),
        }
    )
    wd.status, wd.hot_wallet, wd.nonce = "sending", hot, nonce
    wd.raw_tx, wd.tx_hash, wd.attempts = raw_tx, tx_hash_of(raw_tx), int(wd.attempts or 0) + 1
    await db.flush()
    await db.commit()  # persisted BEFORE broadcast: a crash re-sends this exact transaction
    await chain.send_raw(raw_tx)
    wd.status, wd.broadcast_at = "broadcast", utcnow()
    await db.commit()
    out["sent"] = out.get("sent", 0) + 1


async def run_payout_cycle(
    db: AsyncSession, chain: PayoutChain | None = None, signer: WithdrawalSigner | None = None
) -> dict[str, Any]:
    """One worker pass: advance the in-flight payout, then (if not paused) start the next pending one."""
    if not settings.withdrawals_configured:
        return {"enabled": False}
    chain = chain or JsonRpcPayoutChain()
    out: dict[str, Any] = {"enabled": True}
    await ledger._advisory_xact_lock(db, "withdrawal_payout")  # one payout worker at a time (Postgres)

    in_flight = await _advance_in_flight(db, chain, out)
    if in_flight or await is_paused(db):
        out["paused"] = await is_paused(db)
        return out
    try:
        signer = signer or build_default_signer()
        await _start_next(db, chain, signer, out)
    except NotConfigured:
        out["blocked"] = "signer_not_configured"  # requests stay pending; an admin can cancel (refund) them
    except (SignerError, ChainClientError) as exc:
        await db.rollback()
        out["error"] = str(exc)
        logger.warning("withdrawal payout step failed (will retry): %s", exc)
    return out
