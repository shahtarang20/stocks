"""Provider reputation + slashing (Phase B3).

Reputation here is derived from REAL history only — completed vs failed/timed-out
jobs, verification failures and disputes lost — never from a hand-set number. The
aggregate in ``provider_reputation`` is a cache of that derivation: it can always
be rebuilt by calling :func:`recompute_reputation` again, and every adjustment
appends an immutable :class:`ReputationEvent` so a provider asking "why is my
score 0.31?" gets an auditable answer.

WHY time-decay: a provider that failed ten jobs six months ago and has run clean
since should not be punished forever, and a provider with a great record from a
year ago should not coast on it. Each signal is weighted
``0.5 ** (age_days / REPUTATION_DECAY_DAYS)``, i.e. a true half-life, so the score
tracks recent behaviour. Signals older than :data:`_DECAY_HALF_LIVES` half-lives
contribute less than 1/16 and are dropped from the query entirely.

WHY a Bayesian shape: ``(prior_success + good) / (prior_total + good + bad)``
starts a brand-new provider at the neutral 0.5 used by the scheduler, so an
unknown provider is neither punished nor preferred, and one bad job out of one
cannot drive the score to zero.

MONEY: a slash is a balanced ledger transaction like any other — it debits
``provider_earnings`` and credits ``platform_revenue`` through
:func:`app.services.ledger.post_transaction`. This module never touches
``BillingAccount.balance_minor``.
"""

from __future__ import annotations

import datetime as dt
import decimal
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import ensure_aware, utcnow
from app.models.billing import Dispute, LedgerEntry
from app.models.jobs import Job
from app.models.provider import Provider, ProviderMachine
from app.models.reputation import (
    NEUTRAL_SCORE,
    ProviderReputation,
    ReputationEvent,
    SlashEvent,
)
from app.models.verification import VerificationResult
from app.services import job_fsm as fsm
from app.services import ledger
from app.services.audit import record_audit

logger = logging.getLogger(__name__)

# Bayesian prior — matches app/services/scheduler.py so an unknown provider
# scores exactly the scheduler's neutral 0.5.
_PRIOR_SUCCESS = 1.0
_PRIOR_TOTAL = 2.0

# How many half-lives of history to consider. Beyond this a signal is worth
# < 1/16 of a fresh one; dropping it keeps the query bounded.
_DECAY_HALF_LIVES = 4

# Relative weight of each negative signal, expressed in "failed jobs".
# A proven verification failure is far stronger evidence of bad behaviour than a
# crashed job (which may be the customer's fault), and a lost dispute stronger
# still than a timeout.
_WEIGHT_FAILED_JOB = 1.0
_WEIGHT_TIMED_OUT_JOB = 0.5
_WEIGHT_VERIFICATION_FAILURE = 5.0
_WEIGHT_DISPUTE_LOST = 3.0

_SCORE_QUANT = decimal.Decimal("0.0001")


class ReputationError(Exception):
    """Base for reputation/slashing refusals."""


class SlashRefused(ReputationError):
    """Raised when a slash is rejected before any money moves."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class ReputationSignals:
    """Raw, decay-weighted evidence behind a score (the explainable payload)."""

    completed: int = 0
    failed: int = 0
    timed_out: int = 0
    verification_failures: int = 0
    verification_passes: int = 0
    disputes_lost: int = 0
    good_weight: float = 0.0
    bad_weight: float = 0.0
    score: decimal.Decimal = field(default=NEUTRAL_SCORE)

    def as_factors(self) -> dict:
        """Explainable breakdown — safe to show the provider and an auditor."""
        return {
            "completed": self.completed,
            "failed": self.failed,
            "timed_out": self.timed_out,
            "verification_failures": self.verification_failures,
            "verification_passes": self.verification_passes,
            "disputes_lost": self.disputes_lost,
            "good_weight": round(self.good_weight, 4),
            "bad_weight": round(self.bad_weight, 4),
            "decay_days": settings.REPUTATION_DECAY_DAYS,
        }


# ---------------------------------------------------------------------------
# Derivation from real history
# ---------------------------------------------------------------------------
def _decay_weight(when: dt.datetime | None, now: dt.datetime) -> float:
    """Half-life weight for a signal that happened at ``when``.

    ``ensure_aware`` is mandatory: SQLite drops tzinfo on read, so a naive value
    coming back from the DB must be treated as UTC before any subtraction.
    """
    when = ensure_aware(when)
    if when is None:
        return 1.0
    age_days = max((now - when).total_seconds(), 0.0) / 86400.0
    half_life = max(float(settings.REPUTATION_DECAY_DAYS), 1e-6)
    return 0.5 ** (age_days / half_life)


def _clamp_score(raw: float) -> decimal.Decimal:
    floor = float(settings.REPUTATION_MIN_SCORE)
    bounded = min(1.0, max(floor, raw))
    return decimal.Decimal(repr(bounded)).quantize(
        _SCORE_QUANT, rounding=decimal.ROUND_HALF_UP
    )


async def compute_signals(
    db: AsyncSession, provider_id: uuid.UUID, *, now: dt.datetime | None = None
) -> ReputationSignals:
    """Derive a provider's score from real history WITHOUT writing anything.

    Read-only on purpose so a GET can preview a score for a provider that has no
    persisted aggregate yet.
    """
    now = now or utcnow()
    window_start = now - dt.timedelta(
        days=max(int(settings.REPUTATION_DECAY_DAYS), 1) * _DECAY_HALF_LIVES
    )
    signals = ReputationSignals()

    machine_ids = (
        await db.execute(
            select(ProviderMachine.id).where(ProviderMachine.provider_id == provider_id)
        )
    ).scalars().all()
    if not machine_ids:
        signals.score = _clamp_score(_PRIOR_SUCCESS / _PRIOR_TOTAL)
        return signals

    # 1) Job outcomes. Filtered in Python on the timestamp so the comparison is
    # done on tz-normalized values (SQLite stores naive datetimes).
    job_rows = (
        await db.execute(
            select(Job.status, Job.finished_at).where(
                Job.machine_id.in_(machine_ids), Job.finished_at.is_not(None)
            )
        )
    ).all()
    for job_status, finished_at in job_rows:
        finished_at = ensure_aware(finished_at)
        if finished_at is None or finished_at < window_start:
            continue
        weight = _decay_weight(finished_at, now)
        if job_status == fsm.COMPLETED:
            signals.completed += 1
            signals.good_weight += weight
        elif job_status == fsm.FAILED:
            signals.failed += 1
            signals.bad_weight += weight * _WEIGHT_FAILED_JOB
        elif job_status == fsm.TIMED_OUT:
            # Covers lease expiry: recover_expired_leases parks the job in
            # TIMED_OUT once retries are exhausted.
            signals.timed_out += 1
            signals.bad_weight += weight * _WEIGHT_TIMED_OUT_JOB

    # 2) Verification evidence (challenge / redundancy / model identity).
    verification_rows = (
        await db.execute(
            select(VerificationResult.passed, VerificationResult.created_at)
            .join(Job, VerificationResult.job_id == Job.id)
            .where(Job.machine_id.in_(machine_ids))
        )
    ).all()
    for passed, created_at in verification_rows:
        created_at = ensure_aware(created_at)
        if created_at is None or created_at < window_start:
            continue
        if passed:
            signals.verification_passes += 1
            signals.good_weight += _decay_weight(created_at, now)
        else:
            signals.verification_failures += 1
            signals.bad_weight += (
                _decay_weight(created_at, now) * _WEIGHT_VERIFICATION_FAILURE
            )

    # 3) Disputes LOST — resolved in the customer's favour (money refunded).
    dispute_rows = (
        await db.execute(
            select(Dispute.resolved_at)
            .join(Job, Dispute.job_id == Job.id)
            .where(
                Job.machine_id.in_(machine_ids),
                Dispute.status == "resolved",
                Dispute.refund_amount_minor.is_not(None),
                Dispute.refund_amount_minor > 0,
            )
        )
    ).all()
    for (resolved_at,) in dispute_rows:
        resolved_at = ensure_aware(resolved_at)
        if resolved_at is None or resolved_at < window_start:
            continue
        signals.disputes_lost += 1
        signals.bad_weight += _decay_weight(resolved_at, now) * _WEIGHT_DISPUTE_LOST

    raw = (_PRIOR_SUCCESS + signals.good_weight) / (
        _PRIOR_TOTAL + signals.good_weight + signals.bad_weight
    )
    signals.score = _clamp_score(raw)
    return signals


async def get_reputation_row(
    db: AsyncSession, provider_id: uuid.UUID
) -> ProviderReputation | None:
    return (
        await db.execute(
            select(ProviderReputation).where(
                ProviderReputation.provider_id == provider_id
            )
        )
    ).scalar_one_or_none()


async def recompute_reputation(
    db: AsyncSession,
    provider_id: uuid.UUID,
    *,
    event_kind: str = "decay",
    reason: str | None = None,
    job_id: uuid.UUID | None = None,
    machine_id: uuid.UUID | None = None,
    now: dt.datetime | None = None,
) -> ProviderReputation:
    """Re-derive and PERSIST a provider's reputation, appending an event.

    ``event_kind`` names the signal that triggered the recompute (one of
    :data:`app.models.reputation.REPUTATION_EVENT_KINDS`) so the append-only log
    reads as a history of causes, not of anonymous recalculations. The event is
    always appended — a zero delta is still evidence that the signal was
    processed and changed nothing.
    """
    now = now or utcnow()
    signals = await compute_signals(db, provider_id, now=now)

    row = await get_reputation_row(db, provider_id)
    previous = decimal.Decimal(row.score) if row is not None else NEUTRAL_SCORE
    if row is None:
        # ``provider_reputation.provider_id`` is UNIQUE, so two concurrent
        # first-time recomputes for the same provider both insert and one loses
        # with an IntegrityError. The hot-path caller wraps this in ``safe_hook``,
        # which would SWALLOW that error and silently drop the outcome — a
        # provider failing two jobs at once would be penalised for only one.
        # Insert inside a SAVEPOINT and fall back to the winner's row.
        try:
            async with db.begin_nested():
                row = ProviderReputation(provider_id=provider_id, score=signals.score)
                db.add(row)
                await db.flush()
        except IntegrityError:
            row = await get_reputation_row(db, provider_id)
            if row is None:  # pragma: no cover - the conflict proves it exists
                raise
            previous = decimal.Decimal(row.score)
            row.score = signals.score
    else:
        row.score = signals.score
    row.completed_count = signals.completed
    row.failed_count = signals.failed + signals.timed_out
    row.verification_failures = signals.verification_failures
    row.disputes_lost = signals.disputes_lost
    row.decayed_at = now
    if machine_id is not None:
        row.machine_id = machine_id

    db.add(
        ReputationEvent(
            provider_id=provider_id,
            machine_id=machine_id,
            kind=event_kind,
            delta=(signals.score - previous).quantize(_SCORE_QUANT),
            reason=reason,
            job_id=job_id,
        )
    )
    await db.flush()
    return row


async def record_job_outcome(
    db: AsyncSession, job: Job, *, now: dt.datetime | None = None
) -> ProviderReputation | None:
    """Fold a finished job into its provider's reputation.

    Returns ``None`` when the job never reached a machine (nothing to attribute).
    """
    if job.machine_id is None:
        return None
    machine = await db.get(ProviderMachine, job.machine_id)
    if machine is None:
        return None
    kind = "job_completed" if job.status == fsm.COMPLETED else "job_failed"
    return await recompute_reputation(
        db,
        machine.provider_id,
        event_kind=kind,
        reason=f"job_{job.status}",
        job_id=job.id,
        machine_id=machine.id,
        now=now,
    )


async def record_verification_outcome(
    db: AsyncSession,
    result: VerificationResult,
    *,
    now: dt.datetime | None = None,
) -> ProviderReputation | None:
    """Fold a verification result into the executing provider's reputation."""
    job = await db.get(Job, result.job_id)
    if job is None or job.machine_id is None:
        return None
    machine = await db.get(ProviderMachine, job.machine_id)
    if machine is None:
        return None
    kind = "verification_passed" if result.passed else "verification_failed"
    return await recompute_reputation(
        db,
        machine.provider_id,
        event_kind=kind,
        reason=f"verification_{result.kind}",
        job_id=job.id,
        machine_id=machine.id,
        now=now,
    )


async def reputation_by_machine(
    db: AsyncSession, machine_ids: list[uuid.UUID]
) -> dict[uuid.UUID, float]:
    """Persisted reputation per machine, for the scheduler hot path.

    Reputation is tracked per PROVIDER; every machine of that provider inherits
    the score. Machines whose provider has no persisted row are absent from the
    mapping so the caller can fall back to its own computation.
    """
    if not machine_ids:
        return {}
    rows = (
        await db.execute(
            select(ProviderMachine.id, ProviderReputation.score)
            .join(
                ProviderReputation,
                ProviderReputation.provider_id == ProviderMachine.provider_id,
            )
            .where(ProviderMachine.id.in_(machine_ids))
        )
    ).all()
    return {machine_id: float(score) for machine_id, score in rows}


async def recent_events(
    db: AsyncSession, provider_id: uuid.UUID, *, limit: int = 50
) -> list[ReputationEvent]:
    return list(
        (
            await db.execute(
                select(ReputationEvent)
                .where(ReputationEvent.provider_id == provider_id)
                .order_by(ReputationEvent.created_at.desc(), ReputationEvent.id.desc())
                .limit(limit)
            )
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# Slashing
# ---------------------------------------------------------------------------
async def _job_provider_payout_minor(
    db: AsyncSession, job_id: uuid.UUID, provider_account_id: uuid.UUID
) -> int:
    """Minor units actually credited to the provider for ``job_id``.

    Read from the LEDGER (the capture's credit leg), never from a cached number,
    so the slash cap is anchored to money that really moved.
    """
    from app.models.billing import LedgerTransaction

    total = 0
    rows = (
        await db.execute(
            select(LedgerEntry.amount_minor)
            .join(LedgerTransaction, LedgerEntry.transaction_id == LedgerTransaction.id)
            .where(
                LedgerTransaction.reference_type == "job",
                LedgerTransaction.reference_id == str(job_id),
                LedgerTransaction.kind == "capture",
                LedgerEntry.account_id == provider_account_id,
                LedgerEntry.direction == "credit",
            )
        )
    ).scalars().all()
    for amount in rows:
        total += int(amount)
    return total


async def _prior_slashed_minor(
    db: AsyncSession, provider_id: uuid.UUID, job_id: uuid.UUID | None
) -> int:
    """Minor units already slashed from this provider against the same basis.

    Only job-anchored slashes accumulate. With no ``job_id`` the cap basis is the
    provider's CURRENT earnings balance, which the previous slash already
    reduced, so that basis is self-limiting; summing history against it would
    instead block slashing a provider forever. With a ``job_id`` the basis is the
    job's capture leg, which never changes, so the sum is what makes
    SLASH_MAX_FRACTION a cap rather than a per-call allowance.

    Only rows with a posted ``ledger_txn_id`` count — money that did not move
    must not consume the cap.
    """
    if job_id is None:
        return 0
    total = (
        await db.execute(
            select(func.coalesce(func.sum(SlashEvent.amount_minor), 0)).where(
                SlashEvent.provider_id == provider_id,
                SlashEvent.job_id == job_id,
                SlashEvent.ledger_txn_id.is_not(None),
            )
        )
    ).scalar()
    return int(total or 0)


async def slash_provider(
    db: AsyncSession,
    *,
    provider_id: uuid.UUID,
    amount_minor: int,
    actor_user_id: uuid.UUID,
    reason: str,
    idempotency_key: str,
    job_id: uuid.UUID | None = None,
    ip: str | None = None,
    now: dt.datetime | None = None,
) -> SlashEvent:
    """Claw back provider earnings on PROVEN fraud. Admin-only at the edge.

    Refusals happen BEFORE any money moves:

    * ``invalid_amount``   — non-positive amount;
    * ``provider_not_found``;
    * ``slash_cap_exceeded`` — above ``SLASH_MAX_FRACTION`` of the job's payout
      (or, with no job, of the provider's current earnings balance);
    * :class:`app.services.ledger.InsufficientFunds` — ``provider_earnings`` is a
      no-negative account, so a slash can never overdraw it.

    Idempotent on ``idempotency_key``: a replay returns the SAME SlashEvent and
    moves no further money.
    """
    now = now or utcnow()
    amount_minor = int(amount_minor)
    if amount_minor <= 0:
        raise SlashRefused("invalid_amount")

    provider = await db.get(Provider, provider_id)
    if provider is None:
        raise SlashRefused("provider_not_found")

    provider_acct = await ledger.get_provider_account(db, provider_id)
    platform_rev = await ledger.get_platform_account(db, "platform_revenue")

    fraction = decimal.Decimal(str(settings.SLASH_MAX_FRACTION))
    if job_id is not None:
        basis = await _job_provider_payout_minor(db, job_id, provider_acct.id)
        if basis <= 0:
            raise SlashRefused("no_payout_for_job")
    else:
        basis = int(provider_acct.balance_minor)
    cap = int(
        (decimal.Decimal(basis) * fraction).to_integral_value(
            rounding=decimal.ROUND_FLOOR
        )
    )
    # CUMULATIVE cap (mirrors the refund cap in billing.refund). The basis is
    # recomputed from the capture legs on every call and a slash is a separate
    # ``kind='slash'`` transaction, so the basis never shrinks — without this
    # sum, a second call under a fresh idempotency_key is a fresh, fully-capped
    # slash and SLASH_MAX_FRACTION means nothing beyond "per call".
    prior_slashed = await _prior_slashed_minor(db, provider_id, job_id)
    if prior_slashed + amount_minor > cap:
        raise SlashRefused("slash_cap_exceeded")

    full_key = f"slash:{idempotency_key}"
    # Raises InsufficientFunds (nothing written) if this would overdraw the
    # provider's no-negative earnings account.
    txn = await ledger.post_transaction(
        db,
        kind="slash",
        legs=[
            (provider_acct, "debit", amount_minor),
            (platform_rev, "credit", amount_minor),
        ],
        idempotency_key=full_key,
        reference_type="provider",
        reference_id=str(provider_id),
        actor_user_id=actor_user_id,
    )

    # The ledger is idempotent; so is this row. A replay must not append a second
    # SlashEvent (or a second reputation event) for one movement of money.
    existing = (
        await db.execute(select(SlashEvent).where(SlashEvent.ledger_txn_id == txn.id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    event = SlashEvent(
        provider_id=provider_id,
        job_id=job_id,
        amount_minor=amount_minor,
        currency=provider_acct.currency,
        reason=reason,
        ledger_txn_id=txn.id,
        actor_user_id=actor_user_id,
    )
    db.add(event)
    await db.flush()

    await recompute_reputation(
        db,
        provider_id,
        event_kind="slash",
        reason=reason[:512],
        job_id=job_id,
        now=now,
    )
    await record_audit(
        db,
        action="provider.slash",
        outcome="success",
        actor_user_id=actor_user_id,
        resource_type="provider",
        resource_id=str(provider_id),
        ip=ip,
        reason=f"{reason} (amount_minor={amount_minor}, txn={txn.id})",
    )
    logger.info(
        "slash posted provider=%s amount_minor=%s txn=%s actor=%s",
        provider_id,
        amount_minor,
        txn.id,
        actor_user_id,
    )
    return event
