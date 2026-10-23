"""Double-entry ledger core — THE single posting choke point (Phase 5).

Every money move in the system is a balanced :func:`post_transaction`. Nothing
else mutates ``billing_accounts.balance_minor``. The three invariants:

* INVARIANT 1 — every transaction balances: SUM(debits) == SUM(credits). An
  unbalanced posting is refused (:class:`LedgerImbalance`) and nothing is written.
* INVARIANT 2 — conservation: across the whole ledger SUM(all debits) ==
  SUM(all credits). Follows from INVARIANT 1 holding for every transaction.
* INVARIANT 3 — no negative balances: a posting that would drive a
  ``customer_credits`` or ``holding`` account below zero is refused
  (:class:`InsufficientFunds`). Checked inside the row-locked critical section.

All money is integer minor units (exponent 2) or :class:`decimal.Decimal` — NEVER
float.

LOCKING — SQLite vs Postgres (honest note)
------------------------------------------
On Postgres the affected account rows are locked with ``SELECT ... FOR UPDATE`` so
balance recompute-and-write serializes across processes. SQLite does not honor
``FOR UPDATE`` (a no-op), so in-process concurrency (the test harness, a single
shared connection via StaticPool) is serialized by a module-level ``asyncio.Lock``
wrapping the same read-modify-write. The UNIQUE(idempotency_key) constraint is the
independent backstop against a double post.
"""

from __future__ import annotations

import asyncio
import decimal
import uuid
from collections import defaultdict

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.billing import (
    CURRENCY,
    BillingAccount,
    LedgerEntry,
    LedgerTransaction,
)

# Accounts that may NEVER go negative (INVARIANT 3).
# Accounts that must never go negative. provider_earnings is floored so a payout
# or refund cannot overdraw a provider (platform_revenue is intentionally NOT
# listed: the platform may absorb a refund beyond its collected fee).
_NON_NEGATIVE_TYPES = frozenset({"customer_credits", "holding", "provider_earnings"})
# DEBIT-normal accounts (balance = debits - credits). Everything else is
# CREDIT-normal (balance = credits - debits).
_DEBIT_NORMAL_TYPES = frozenset({"external"})

# A single in-process posting lock (see module docstring).
_posting_lock = asyncio.Lock()

_CENTS = decimal.Decimal("0.01")


class LedgerError(Exception):
    """Base for ledger posting refusals."""


class LedgerImbalance(LedgerError):
    """Raised when a transaction's debits != credits (INVARIANT 1)."""


class InsufficientFunds(LedgerError):
    """Raised when a posting would drive a guarded account negative (INVARIANT 3)."""

    def __init__(self, account_type: str, shortfall_minor: int):
        super().__init__(f"insufficient_funds:{account_type}")
        self.account_type = account_type
        self.shortfall_minor = shortfall_minor


# ---------------------------------------------------------------------------
# Money utilities — Decimal <-> integer minor units (NEVER float)
# ---------------------------------------------------------------------------
def _scale() -> int:
    return 10 ** settings.BILLING_CURRENCY_EXPONENT


def to_minor(value: decimal.Decimal | int | str) -> int:
    """Convert a decimal amount to integer minor units (ROUND_HALF_UP, exp 2)."""
    d = decimal.Decimal(value).quantize(_CENTS, rounding=decimal.ROUND_HALF_UP)
    return int(d * _scale())


def from_minor(amount_minor: int) -> decimal.Decimal:
    """Convert integer minor units back to a 2dp Decimal."""
    return (decimal.Decimal(int(amount_minor)) / _scale()).quantize(_CENTS)


def minor_to_str(amount_minor: int) -> str:
    """Format minor units as a decimal string, e.g. 1234 -> '12.34'."""
    return f"{from_minor(amount_minor)}"


def _balance_delta(account_type: str, direction: str, amount_minor: int) -> int:
    """Signed change to the cached balance for one entry on ``account_type``."""
    debit_normal = account_type in _DEBIT_NORMAL_TYPES
    if direction == "debit":
        return amount_minor if debit_normal else -amount_minor
    return -amount_minor if debit_normal else amount_minor


# ---------------------------------------------------------------------------
# Posting primitive — the single choke point
# ---------------------------------------------------------------------------
async def post_transaction(
    db: AsyncSession,
    *,
    kind: str,
    legs: list[tuple[BillingAccount, str, int]],
    idempotency_key: str,
    reference_type: str | None = None,
    reference_id: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    currency: str = CURRENCY,
) -> LedgerTransaction:
    """Post a balanced double-entry transaction.

    ``legs`` is a list of ``(account, direction, amount_minor)`` with
    ``amount_minor > 0`` and ``direction`` in ``{'debit', 'credit'}``.

    1. refuse an unbalanced transaction (INVARIANT 1);
    2. idempotent on ``idempotency_key`` — a repeat returns the existing
       transaction with NO second effect;
    3. in ONE DB transaction, row-lock the affected accounts, insert the txn +
       entries, update cached balances, and refuse (INVARIANT 3) if a guarded
       account would go negative.
    """
    if not legs:
        raise LedgerImbalance("empty_transaction")
    for _acct, direction, amount in legs:
        if direction not in ("debit", "credit"):
            raise LedgerImbalance(f"bad_direction:{direction}")
        if amount is None or int(amount) <= 0:
            raise LedgerImbalance("non_positive_amount")

    debits = sum(int(a) for _ac, d, a in legs if d == "debit")
    credits = sum(int(a) for _ac, d, a in legs if d == "credit")
    if debits != credits:
        # INVARIANT 1 — refuse; nothing is written.
        raise LedgerImbalance(f"imbalanced:{debits}!={credits}")

    async with _posting_lock:
        # (2) Idempotency: a prior posting with this key wins, no second effect.
        existing = (
            await db.execute(
                select(LedgerTransaction).where(
                    LedgerTransaction.idempotency_key == idempotency_key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        # (3) Row-lock the affected accounts (Postgres; no-op on SQLite).
        account_ids = list({acct.id for acct, _d, _a in legs})
        locked = (
            await db.execute(
                select(BillingAccount)
                .where(BillingAccount.id.in_(account_ids))
                .with_for_update()
            )
        ).scalars().all()
        by_id = {a.id: a for a in locked}

        # Aggregate the net balance delta per account across all legs.
        deltas: dict[uuid.UUID, int] = defaultdict(int)
        for acct, direction, amount in legs:
            account = by_id[acct.id]
            deltas[account.id] += _balance_delta(account.type, direction, int(amount))

        # Refuse BEFORE writing if a guarded account would go negative.
        for account_id, delta in deltas.items():
            account = by_id[account_id]
            new_balance = account.balance_minor + delta
            if account.type in _NON_NEGATIVE_TYPES and new_balance < 0:
                raise InsufficientFunds(account.type, -new_balance)

        txn = LedgerTransaction(
            kind=kind,
            status="posted",
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
            actor_user_id=actor_user_id,
        )
        db.add(txn)
        await db.flush()

        for acct, direction, amount in legs:
            db.add(
                LedgerEntry(
                    transaction_id=txn.id,
                    account_id=acct.id,
                    direction=direction,
                    amount_minor=int(amount),
                    currency=currency,
                )
            )
        for account_id, delta in deltas.items():
            by_id[account_id].balance_minor += delta

        try:
            await db.flush()
        except IntegrityError:
            # UNIQUE(idempotency_key) backstop under a race — return the winner.
            await db.rollback()
            return (
                await db.execute(
                    select(LedgerTransaction).where(
                        LedgerTransaction.idempotency_key == idempotency_key
                    )
                )
            ).scalar_one()
        return txn


# ---------------------------------------------------------------------------
# Account helpers
# ---------------------------------------------------------------------------
async def get_platform_account(db: AsyncSession, account_type: str) -> BillingAccount:
    """Fetch (or create) a singleton platform-side account by type.

    Covers external, holding, platform_revenue and payout_pending. Migrations
    seed these; tests use create_all (no seed), so create-if-missing here.
    """
    owner_type = {
        "external": "external",
        "holding": "holding",
        "platform_revenue": "platform",
        "payout_pending": "platform",
    }[account_type]
    account = (
        await db.execute(
            select(BillingAccount).where(
                BillingAccount.type == account_type,
                BillingAccount.owner_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if account is None:
        account = BillingAccount(
            owner_type=owner_type, owner_id=None, type=account_type, currency=CURRENCY
        )
        db.add(account)
        await db.flush()
    return account


async def get_provider_account(db: AsyncSession, provider_id: uuid.UUID) -> BillingAccount:
    account = (
        await db.execute(
            select(BillingAccount).where(
                BillingAccount.owner_type == "provider",
                BillingAccount.owner_id == provider_id,
                BillingAccount.type == "provider_earnings",
            )
        )
    ).scalar_one_or_none()
    if account is None:
        # Same first-touch race as get_or_create_customer_account: a new
        # provider's earnings and payouts pages load together, both saw "none"
        # and both INSERTed. Serialize, then look again.
        await _advisory_xact_lock(db, f"provider_account:{provider_id}")
        account = (
            await db.execute(
                select(BillingAccount).where(
                    BillingAccount.owner_type == "provider",
                    BillingAccount.owner_id == provider_id,
                    BillingAccount.type == "provider_earnings",
                )
            )
        ).scalar_one_or_none()
    if account is None:
        account = BillingAccount(
            owner_type="provider",
            owner_id=provider_id,
            type="provider_earnings",
            currency=CURRENCY,
        )
        db.add(account)
        await db.flush()
    return account


async def _get_customer_account_row(
    db: AsyncSession, user_id: uuid.UUID
) -> BillingAccount | None:
    return (
        await db.execute(
            select(BillingAccount).where(
                BillingAccount.owner_type == "customer",
                BillingAccount.owner_id == user_id,
                BillingAccount.type == "customer_credits",
            )
        )
    ).scalar_one_or_none()


async def lock_customer(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Serialize a customer's money-moving requests for the rest of the transaction.

    Taken FIRST in a request (before any machine/account row locks) so two concurrent
    submits by the same customer cannot interleave account creation with the
    scheduler's row locks and deadlock. Postgres only; no-op elsewhere (SQLite).
    """
    await _advisory_xact_lock(db, f"customer:{user_id}")


async def _advisory_xact_lock(db: AsyncSession, key: str) -> None:
    """Transaction-scoped advisory lock on ``key``. Postgres only; no-op elsewhere."""
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": key},
        )


async def get_or_create_customer_account(
    db: AsyncSession, user_id: uuid.UUID
) -> BillingAccount:
    """Return the customer's credits account, creating it (and granting the signup
    promo credits via a credit_purchase from external) on first interaction."""
    account = await _get_customer_account_row(db, user_id)
    if account is not None:
        return account

    # A new customer's first page load fires several billing calls at once
    # (balance and ledger). Without serializing here, each saw "no account",
    # each tried to INSERT, and the loser hit uq_billing_accounts_owner_type and
    # surfaced as a 500. Take the per-customer lock, then look again: the loser
    # waits for the winner's commit and finds the row instead of inserting.
    await lock_customer(db, user_id)
    account = await _get_customer_account_row(db, user_id)
    if account is not None:
        return account

    account = BillingAccount(
        owner_type="customer",
        owner_id=user_id,
        type="customer_credits",
        currency=CURRENCY,
    )
    db.add(account)
    await db.flush()

    grant = int(settings.BILLING_SIGNUP_GRANT_MINOR)
    if grant > 0:
        external = await get_platform_account(db, "external")
        await post_transaction(
            db,
            kind="credit_purchase",
            legs=[
                (external, "debit", grant),
                (account, "credit", grant),
            ],
            idempotency_key=f"signup_grant:{user_id}",
            reference_type="signup_grant",
            reference_id=str(user_id),
        )
        await db.refresh(account)
    return account


# ---------------------------------------------------------------------------
# Reconciliation (INVARIANT 2 surfaced)
# ---------------------------------------------------------------------------
async def ledger_totals(db: AsyncSession) -> tuple[int, int]:
    """Return (total_debits_minor, total_credits_minor) across ALL entries."""
    total_debits = (
        await db.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(
                LedgerEntry.direction == "debit"
            )
        )
    ).scalar_one()
    total_credits = (
        await db.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(
                LedgerEntry.direction == "credit"
            )
        )
    ).scalar_one()
    return int(total_debits), int(total_credits)
