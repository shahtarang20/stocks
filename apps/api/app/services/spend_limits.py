"""Spend limits: ledger-derived budget caps + exactly-once threshold alerts (Phase B4).

WHY the period spend is recomputed FROM THE LEDGER on every check instead of
kept in a counter column: a cached counter is a second source of truth that
drifts the moment a refund, a release or a manual adjustment is posted, and a
budget that silently drifts is worse than no budget. The ledger already is the
authoritative record of every money movement (INVARIANT 2), so the cap is a
pure function of it.

WHY :func:`spend_guard` exists next to :func:`check_spend_allowed`: checking a
cap and then spending is a classic TOCTOU — two concurrent submits both read
"spent 90 of 100", both pass, and the customer lands at 180. The guard holds the
serialization primitives across BOTH the check and the caller's ledger posting:

* Postgres — ``ledger.lock_customer`` takes a transaction-scoped advisory lock on
  the customer, so the check and the subsequent ``hold`` are serialized across
  processes for the whole request transaction.
* SQLite / single-process — advisory locks are a no-op there, so an in-process
  per-customer :class:`asyncio.Lock` serializes the same critical section (the
  same shape the ledger itself uses for its posting lock).

Money is integer minor units — never float.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.models.billing import BillingAccount, LedgerEntry, LedgerTransaction
from app.models.jobs import Job
from app.models.spend import SPEND_PERIODS, SPEND_SCOPES, SpendAlert, SpendLimit
from app.services import ledger

__all__ = [
    "SPEND_PERIODS",
    "SPEND_SCOPES",
    "SpendLimitExceeded",
    "check_spend_allowed",
    "spend_guard",
    "period_key",
    "period_start",
    "period_spend_minor",
    "record_spend_alerts",
    "limit_usage",
]

# Ledger transaction kinds that represent customer spend (or its reversal).
# ``credit_purchase`` is deliberately absent: topping up is not negative spend.
SPEND_TXN_KINDS = ("hold", "capture", "release", "refund", "adjustment")

# Per-customer in-process gates. WeakValueDictionary so the map does not grow
# without bound; a lock stays alive while any waiter holds a strong reference.
_customer_gates: "weakref.WeakValueDictionary[uuid.UUID, asyncio.Lock]" = (
    weakref.WeakValueDictionary()
)
_gates_guard = asyncio.Lock()


class SpendLimitExceeded(Exception):
    """Raised when a spend would breach an enforcing :class:`SpendLimit`.

    The caller maps this to HTTP 403 ``spend_limit_exceeded`` (a policy refusal,
    not a funding problem — 402 would wrongly suggest "top up and retry").
    """

    def __init__(self, limit: SpendLimit, spent_minor: int, requested_minor: int) -> None:
        super().__init__("spend_limit_exceeded")
        self.limit = limit
        self.scope = limit.scope
        self.period = limit.period
        self.spent_minor = int(spent_minor)
        self.requested_minor = int(requested_minor)
        self.limit_minor = int(limit.limit_minor)


# ---------------------------------------------------------------------------
# Period windows
# ---------------------------------------------------------------------------
def period_start(period: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """Inclusive start of the current window, or ``None`` for ``total``."""
    moment = ensure_aware(now) or utcnow()
    if period == "daily":
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "monthly":
        return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "total":
        return None
    raise ValueError(f"unknown spend period: {period!r}")


def period_key(period: str, now: dt.datetime | None = None) -> str:
    """Stable bucket id for the current window — the alert idempotency component."""
    moment = ensure_aware(now) or utcnow()
    if period == "daily":
        return moment.strftime("%Y-%m-%d")
    if period == "monthly":
        return moment.strftime("%Y-%m")
    if period == "total":
        return "total"
    raise ValueError(f"unknown spend period: {period!r}")


# ---------------------------------------------------------------------------
# Ledger-derived spend
# ---------------------------------------------------------------------------
async def _customer_account(
    db: AsyncSession, customer_id: uuid.UUID
) -> BillingAccount | None:
    """The customer's credits account, WITHOUT creating it.

    Reading a budget must never have the side effect of opening an account (and
    granting signup credits), so this deliberately does not use
    ``ledger.get_or_create_customer_account``.
    """
    return (
        await db.execute(
            select(BillingAccount).where(
                BillingAccount.owner_type == "customer",
                BillingAccount.owner_id == customer_id,
                BillingAccount.type == "customer_credits",
            )
        )
    ).scalar_one_or_none()


async def _project_job_references(db: AsyncSession, project_id: uuid.UUID) -> list[str]:
    """``reference_id`` strings of EVERY job in one project.

    Deliberately NOT narrowed to one customer: a project is a shared, org-scoped
    budget envelope, so its spend is the spend of all its jobs. Filtering by the
    limit creator would turn a project budget into a per-user budget that any
    other member could exceed without limit.

    Ledger transactions reference a job as ``reference_id = str(job.id)``; the
    comparison is done on the string form because ``Uuid`` columns render
    differently per dialect (SQLite stores 32 hex chars, Postgres stores a uuid),
    so a SQL cast would not match reliably.
    """
    rows = (
        await db.execute(select(Job.id).where(Job.project_id == project_id))
    ).scalars().all()
    return [str(job_id) for job_id in rows]


async def period_spend_minor(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    period: str,
    scope: str = "customer",
    scope_id: uuid.UUID | None = None,
    now: dt.datetime | None = None,
) -> int:
    """Net spend in minor units for one scope over the current period.

    Net = (debits to the customer's credits account) - (credits back to it),
    restricted to :data:`SPEND_TXN_KINDS`. A released hold or a refund therefore
    reduces the period's spend, which is what a customer expects of a budget.
    Never negative (a refund-heavy window floors at zero).
    """
    since = period_start(period, now)
    conditions = [LedgerTransaction.kind.in_(SPEND_TXN_KINDS)]
    if since is not None:
        conditions.append(LedgerTransaction.created_at >= since)

    if scope == "project":
        if scope_id is None:
            return 0
        references = await _project_job_references(db, scope_id)
        if not references:
            return 0
        # Project spend aggregates across EVERY member's credits account, not
        # just the caller's — see _project_job_references.
        conditions.append(
            LedgerEntry.account_id.in_(
                select(BillingAccount.id).where(
                    BillingAccount.type == "customer_credits"
                )
            )
        )
        conditions.append(LedgerTransaction.reference_type == "job")
        conditions.append(LedgerTransaction.reference_id.in_(references))
    elif scope == "customer":
        account = await _customer_account(db, customer_id)
        if account is None:
            return 0
        conditions.append(LedgerEntry.account_id == account.id)
    else:
        # api_key scope is not attributable: nothing in the schema links a job
        # (and therefore a ledger transaction) back to the API key that created
        # it. Refusing to guess keeps the number honest.
        raise ValueError(f"spend is not attributable for scope {scope!r}")

    rows = (
        await db.execute(
            select(
                LedgerEntry.direction,
                func.coalesce(func.sum(LedgerEntry.amount_minor), 0),
            )
            .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
            .where(*conditions)
            .group_by(LedgerEntry.direction)
        )
    ).all()

    totals = {direction: int(amount) for direction, amount in rows}
    net = totals.get("debit", 0) - totals.get("credit", 0)
    return max(net, 0)


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------
async def _applicable_limits(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    project_id: uuid.UUID | None,
) -> list[SpendLimit]:
    """Active limits that govern this spend: the customer's, plus the project's."""
    scope_pairs: list[tuple[str, uuid.UUID]] = [("customer", customer_id)]
    if project_id is not None:
        scope_pairs.append(("project", project_id))

    limits: list[SpendLimit] = []
    for scope, scope_id in scope_pairs:
        conditions = [
            SpendLimit.scope == scope,
            SpendLimit.scope_id == scope_id,
            SpendLimit.active.is_(True),
        ]
        if scope == "customer":
            # A customer-scoped limit is the customer's own; scope_id already
            # identifies them, and owner_id keeps the row honest.
            conditions.append(SpendLimit.owner_id == customer_id)
        # A PROJECT-scoped limit governs the project, whoever created it. Keeping
        # the owner_id filter here made a project budget apply only to its author,
        # so any other member's jobs were never checked against it at all.
        rows = (await db.execute(select(SpendLimit).where(*conditions))).scalars().all()
        limits.extend(rows)
    return limits


async def check_spend_allowed(
    db: AsyncSession,
    customer_id: uuid.UUID,
    project_id: uuid.UUID | None,
    amount_minor: int,
    *,
    now: dt.datetime | None = None,
) -> None:
    """Refuse a spend that would breach an enforcing budget.

    MUST be called inside the caller's per-customer critical section — in the API
    that means after ``ledger.lock_customer`` and, in-process, inside
    :func:`spend_guard`. Alert-only limits (``enforce=False``) never raise; they
    still fire their thresholds from :func:`record_spend_alerts`.
    """
    amount = max(int(amount_minor), 0)
    moment = ensure_aware(now) or utcnow()

    for limit in await _applicable_limits(
        db, customer_id=customer_id, project_id=project_id
    ):
        if not limit.enforce:
            continue
        spent = await period_spend_minor(
            db,
            customer_id=customer_id,
            period=limit.period,
            scope=limit.scope,
            scope_id=limit.scope_id,
            now=moment,
        )
        if spent + amount > int(limit.limit_minor):
            raise SpendLimitExceeded(limit, spent, amount)


async def _gate_for(customer_id: uuid.UUID) -> asyncio.Lock:
    async with _gates_guard:
        gate = _customer_gates.get(customer_id)
        if gate is None:
            gate = asyncio.Lock()
            _customer_gates[customer_id] = gate
        return gate


@asynccontextmanager
async def spend_guard(
    db: AsyncSession,
    customer_id: uuid.UUID,
    project_id: uuid.UUID | None,
    amount_minor: int,
    *,
    now: dt.datetime | None = None,
    alerts: bool = True,
) -> AsyncIterator[None]:
    """Race-safe cap enforcement around a money movement.

    Usage (the job-submit path)::

        async with spend_guard(db, current.id, body.project_id, authorized_minor):
            await schedule_job(db, job)   # posts the hold
            await db.commit()             # <- REQUIRED, see below

    The check and the caller's posting happen under the same per-customer lock,
    so two concurrent submits cannot both observe the pre-spend balance.

    THE CALLER MUST COMMIT INSIDE THE GUARDED BLOCK. On Postgres the advisory
    lock taken by ``ledger.lock_customer`` is transaction-scoped and would hold
    until commit anyway; on SQLite / in a single process the in-process gate is
    all there is, and it is released when this block exits. Committing inside
    means the next waiter reads a ledger that already contains this spend.

    Raises :class:`SpendLimitExceeded` BEFORE yielding when the cap is breached.
    Pass ``alerts=False`` when the caller fires threshold alerts itself.
    """
    gate = await _gate_for(customer_id)
    async with gate:
        await ledger.lock_customer(db, customer_id)
        await check_spend_allowed(db, customer_id, project_id, amount_minor, now=now)
        yield
        if alerts:
            # Only on success: the spend is now in the ledger, so the thresholds
            # are evaluated against the real post-spend figure.
            await record_spend_alerts(
                db, customer_id=customer_id, project_id=project_id, now=now
            )


# ---------------------------------------------------------------------------
# Threshold alerts — exactly once per (limit, threshold, period)
# ---------------------------------------------------------------------------
def _thresholds_for(limit: SpendLimit) -> list[int]:
    """Per-limit thresholds, falling back to the platform defaults."""
    raw = limit.alert_thresholds or []
    values: set[int] = set()
    for item in raw:
        try:
            pct = int(item)
        except (TypeError, ValueError):
            continue
        if 0 < pct <= 1000:
            values.add(pct)
    return sorted(values) if values else list(settings.spend_alert_thresholds)


async def record_spend_alerts(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    now: dt.datetime | None = None,
) -> list[SpendAlert]:
    """Fire every newly-crossed threshold exactly once per period.

    The UNIQUE ``(spend_limit_id, threshold_pct, period_key)`` constraint is the
    idempotency key, not application bookkeeping: the INSERT runs in a SAVEPOINT
    so a concurrent duplicate surfaces as an ``IntegrityError`` that is swallowed
    without poisoning the caller's transaction. Returns only the alerts THIS call
    created, so the caller can hand exactly those to a notifier.
    """
    moment = ensure_aware(now) or utcnow()
    created: list[SpendAlert] = []

    for limit in await _applicable_limits(
        db, customer_id=customer_id, project_id=project_id
    ):
        limit_minor = int(limit.limit_minor)
        if limit_minor <= 0:
            continue
        spent = await period_spend_minor(
            db,
            customer_id=customer_id,
            period=limit.period,
            scope=limit.scope,
            scope_id=limit.scope_id,
            now=moment,
        )
        key = period_key(limit.period, moment)
        for pct in _thresholds_for(limit):
            # Integer comparison — no float percentage math on money.
            if spent * 100 < pct * limit_minor:
                continue
            alert = SpendAlert(
                spend_limit_id=limit.id,
                threshold_pct=pct,
                period_key=key,
                spent_minor=spent,
                limit_minor=limit_minor,
            )
            try:
                async with db.begin_nested():
                    db.add(alert)
                    await db.flush()
            except IntegrityError:
                # Already fired for this (limit, threshold, period). Exactly once.
                continue
            created.append(alert)
    return created


async def limit_usage(
    db: AsyncSession, limit: SpendLimit, *, now: dt.datetime | None = None
) -> dict:
    """Current-period usage for one limit, for the owner-facing API."""
    moment = ensure_aware(now) or utcnow()
    spent = await period_spend_minor(
        db,
        customer_id=limit.owner_id,
        period=limit.period,
        scope=limit.scope,
        scope_id=limit.scope_id,
        now=moment,
    )
    limit_minor = int(limit.limit_minor)
    return {
        "period_key": period_key(limit.period, moment),
        "spent_minor": spent,
        "spent": ledger.minor_to_str(spent),
        "limit_minor": limit_minor,
        "limit": ledger.minor_to_str(limit_minor),
        "remaining_minor": max(limit_minor - spent, 0),
        "remaining": ledger.minor_to_str(max(limit_minor - spent, 0)),
        # Integer percent, rounded down; avoids float money math.
        "used_pct": (spent * 100 // limit_minor) if limit_minor > 0 else 0,
    }
