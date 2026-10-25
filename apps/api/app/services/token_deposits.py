"""Credit the ledger for COMPUTE tokens sent to the platform deposit address (ADR-0004, stage 1).

The rules, each of which exists to keep the chain and the ledger from disagreeing:

* A transfer is credited **only** if it came from a wallet the user proved they control
  (``LinkedWallet``). Anything else is recorded as ``unclaimed`` and credits nothing.
* Each on-chain transfer is recorded **once** (unique on chain, tx hash, log index), so rescanning a
  block, or two scanners running together, cannot credit it twice.
* Only blocks at least ``TOKEN_DEPOSIT_CONFIRMATIONS`` deep are read, so a block that gets
  reorganised away is never credited.
* Credits are computed in integers: tokens x the configured rate, rounded **down** to a cent.
* The credit is a balanced ledger posting (external -> customer), the same shape as a credit purchase.
* ``reconcile`` compares the chain with the ledger and reports any difference.

Withdrawals (credits back to tokens) are deliberately not here.
"""

from __future__ import annotations

import decimal
import logging
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.billing import LedgerEntry
from app.models.system import SystemConfiguration
from app.models.token_deposit import TokenDeposit
from app.models.wallet import LinkedWallet
from app.services import ledger
from app.services.chain_reconciler import JsonRpcChainClient

logger = logging.getLogger(__name__)

TOKEN_DECIMALS = 18
CURSOR_KEY = "token_deposit_cursor"
_UNIT = decimal.Decimal(10) ** TOKEN_DECIMALS


class ChainReader(Protocol):
    async def latest_block(self) -> int: ...
    async def transfer_logs(self, token_address: str, to_address: str, from_block: int, to_block: int) -> list[dict[str, Any]]: ...
    async def token_balance(self, token_address: str, owner_address: str) -> int: ...


def tokens_from_raw(raw: int) -> decimal.Decimal:
    return decimal.Decimal(int(raw)) / _UNIT


def credits_minor_for(raw: int) -> int:
    """Credits (in cents) for a deposit: tokens x rate, rounded DOWN so the platform never over-credits."""
    rate = decimal.Decimal(settings.TOKEN_CREDITS_PER_TOKEN)
    credits = tokens_from_raw(raw) * rate
    return int((credits * 100).to_integral_value(rounding=decimal.ROUND_FLOOR))


async def _get_cursor(db: AsyncSession) -> int:
    row = await db.get(SystemConfiguration, CURSOR_KEY)
    if row is None:
        return max(0, int(settings.TOKEN_DEPOSIT_FROM_BLOCK))
    return int(row.value.get("next_block", settings.TOKEN_DEPOSIT_FROM_BLOCK))


async def _set_cursor(db: AsyncSession, next_block: int) -> None:
    row = await db.get(SystemConfiguration, CURSOR_KEY)
    if row is None:
        db.add(SystemConfiguration(key=CURSOR_KEY, value={"next_block": int(next_block)}))
    else:
        row.value = {"next_block": int(next_block)}
    await db.flush()


async def process_transfer(db: AsyncSession, log: dict[str, Any]) -> TokenDeposit | None:
    """Record one transfer and, if it qualifies, credit the sender's account. Returns None if seen before."""
    chain_id = settings.wallet_chain_id
    raw = int(log["amount"])
    sender = log["from_address"].lower()
    tokens = tokens_from_raw(raw)

    status, reason, user_id, credits = "unclaimed", "sender_not_linked", None, None
    wallet = (
        await db.execute(
            select(LinkedWallet).where(LinkedWallet.chain_id == chain_id, LinkedWallet.address == sender)
        )
    ).scalar_one_or_none()
    if wallet is not None:
        user_id = wallet.user_id
        lo = decimal.Decimal(settings.TOKEN_DEPOSIT_MIN_TOKENS)
        hi = decimal.Decimal(settings.TOKEN_DEPOSIT_MAX_TOKENS)
        if tokens < lo:
            status, reason = "rejected", "below_minimum"
        elif tokens > hi:
            status, reason = "rejected", "above_maximum"
        else:
            credits = credits_minor_for(raw)
            if credits <= 0:
                status, reason = "rejected", "too_small"
            else:
                status, reason = "credited", None

    dep = TokenDeposit(
        chain_id=chain_id,
        tx_hash=log["tx_hash"].lower(),
        log_index=int(log["log_index"]),
        block_number=int(log["block_number"]),
        token_address=settings.COMPUTE_TOKEN_ADDRESS.lower(),
        from_address=sender,
        to_address=settings.TOKEN_DEPOSIT_ADDRESS.lower(),
        amount_raw=str(raw),
        status=status,
        reason=reason,
        user_id=user_id if status != "unclaimed" else None,
        credits_minor=credits if status == "credited" else None,
    )
    try:
        # Savepoint: the unique key makes a repeat insert fail cleanly without poisoning the session,
        # and the ledger posting below is rolled back together with the row if anything goes wrong.
        async with db.begin_nested():
            db.add(dep)
            await db.flush()
            if status == "credited":
                account = await ledger.get_or_create_customer_account(db, user_id)
                external = await ledger.get_platform_account(db, "external")
                txn = await ledger.post_transaction(
                    db,
                    kind="credit_purchase",
                    legs=[(external, "debit", credits), (account, "credit", credits)],
                    idempotency_key=f"token_deposit:{chain_id}:{dep.tx_hash}:{dep.log_index}",
                    reference_type="token_deposit",
                    # The ledger's reference column is 64 characters; tx hash + log index is longer,
                    # so point at the deposit row (a UUID) instead. SQLite never complains about this.
                    reference_id=str(dep.id),
                    actor_user_id=user_id,
                )
                dep.ledger_txn_id = txn.id
                await db.flush()
    except IntegrityError:
        return None
    return dep


async def scan_deposits(db: AsyncSession, client: ChainReader | None = None) -> dict[str, Any]:
    """Read newly confirmed transfers into the deposit address and credit them. Commits per chunk."""
    if not settings.token_deposits_enabled:
        return {"enabled": False}
    client = client or JsonRpcChainClient()
    token, deposit = settings.COMPUTE_TOKEN_ADDRESS, settings.TOKEN_DEPOSIT_ADDRESS
    chunk = max(1, int(settings.CHAIN_LOG_CHUNK_BLOCKS))

    latest = await client.latest_block()
    safe_head = latest - int(settings.TOKEN_DEPOSIT_CONFIRMATIONS)
    counts = {"credited": 0, "unclaimed": 0, "rejected": 0, "already_seen": 0}
    scanned_to = None
    while True:
        # One scanner at a time (Postgres), and re-read the cursor in case another one moved it.
        await ledger._advisory_xact_lock(db, "token_deposit_scan")
        start = await _get_cursor(db)
        if start > safe_head:
            await db.commit()
            break
        end = min(start + chunk - 1, safe_head)
        logs = await client.transfer_logs(token, deposit, start, end)
        for log in sorted(logs, key=lambda l: (l["block_number"], l["log_index"])):
            dep = await process_transfer(db, log)
            if dep is None:
                counts["already_seen"] += 1
            else:
                counts[dep.status] += 1
        await _set_cursor(db, end + 1)
        await db.commit()
        scanned_to = end
    return {"enabled": True, "latest_block": latest, "scanned_to_block": scanned_to, "safe_head": safe_head, **counts}


async def reconcile(db: AsyncSession, client: ChainReader | None = None) -> dict[str, Any]:
    """Compare the chain with the ledger. ``ok`` is false if either side disagrees."""
    if not settings.token_deposits_enabled:
        return {"enabled": False}
    client = client or JsonRpcChainClient()
    on_chain = await client.token_balance(settings.COMPUTE_TOKEN_ADDRESS, settings.TOKEN_DEPOSIT_ADDRESS)

    rows = (await db.execute(select(TokenDeposit))).scalars().all()
    total = {"credited": 0, "unclaimed": 0, "rejected": 0}
    credits_issued = 0
    txn_ids = []
    for r in rows:
        total[r.status] += int(r.amount_raw)
        if r.status == "credited":
            credits_issued += int(r.credits_minor or 0)
            txn_ids.append(r.ledger_txn_id)
    recorded = sum(total.values())

    ledger_credits = 0
    if txn_ids:
        ledger_credits = int(
            (
                await db.execute(
                    select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(
                        LedgerEntry.transaction_id.in_(txn_ids), LedgerEntry.direction == "credit"
                    )
                )
            ).scalar_one()
        )

    # The deposit address must hold at least what we have recorded as received. More is normal: it
    # includes transfers still inside the confirmation window. Less means credits were issued against
    # tokens that are no longer there (a reorg, or a withdrawal from the address): investigate.
    tokens_ok = on_chain >= recorded
    ledger_ok = ledger_credits == credits_issued
    return {
        "enabled": True,
        "ok": bool(tokens_ok and ledger_ok),
        "tokens_ok": tokens_ok,
        "ledger_ok": ledger_ok,
        "deposit_address": settings.TOKEN_DEPOSIT_ADDRESS,
        "on_chain_balance": str(tokens_from_raw(on_chain)),
        "recorded_deposits": str(tokens_from_raw(recorded)),
        "not_yet_recorded": str(tokens_from_raw(max(0, on_chain - recorded))),
        "credited_tokens": str(tokens_from_raw(total["credited"])),
        "unclaimed_tokens": str(tokens_from_raw(total["unclaimed"])),
        "rejected_tokens": str(tokens_from_raw(total["rejected"])),
        "credits_issued": ledger.minor_to_str(credits_issued),
        "ledger_credits": ledger.minor_to_str(ledger_credits),
        "deposits": len(rows),
    }
