"""Provider reputation + admin slashing endpoints (Phase B3).

AUTHZ shape, matching the rest of the API:

* a provider reads only THEIR OWN reputation — another provider's id returns 404
  (never 403), so the endpoint does not confirm that the id exists;
* an administrator may read any provider's reputation (support + adjudication);
* slashing is ``administrator``-only, idempotent, and every attempt — accepted or
  refused — is reconstructable from the ledger + audit log.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps import client_ip, get_current_user, require_roles
from app.models.provider import Provider
from app.models.reputation import ProviderReputation
from app.schemas.auth import CurrentUser
from app.schemas.reputation import (
    ReputationEventOut,
    ReputationOut,
    SlashCreate,
    SlashOut,
)
from app.services import ledger
from app.services import reputation as reputation_svc
from app.services.audit import record_audit
from app.services.provider import get_provider_by_user

router = APIRouter(prefix="/v1", tags=["reputation"])

_PROVIDER_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found"
)
_NOT_A_PROVIDER = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN, detail="Not registered as a provider"
)


async def _visible_provider(
    db: AsyncSession, provider_id: uuid.UUID, current: CurrentUser
) -> Provider:
    """Load a provider the caller is allowed to see, else 404.

    404 (not 403) for a provider someone else owns: an id-probing caller must not
    learn which provider ids exist.
    """
    provider = await db.get(Provider, provider_id)
    if provider is None:
        raise _PROVIDER_NOT_FOUND
    if provider.user_id != current.id and "administrator" not in current.roles:
        raise _PROVIDER_NOT_FOUND
    return provider


async def _shape(db: AsyncSession, provider_id: uuid.UUID) -> ReputationOut:
    """Build the response WITHOUT writing: a GET never mutates the aggregate.

    When no aggregate exists yet the score is derived live from history, and
    ``persisted`` is False so the caller can tell the difference.
    """
    row: ProviderReputation | None = await reputation_svc.get_reputation_row(
        db, provider_id
    )
    signals = await reputation_svc.compute_signals(db, provider_id)
    if row is None:
        return ReputationOut(
            provider_id=provider_id,
            score=f"{signals.score:.4f}",
            completed_count=signals.completed,
            failed_count=signals.failed + signals.timed_out,
            verification_failures=signals.verification_failures,
            disputes_lost=signals.disputes_lost,
            persisted=False,
            factors=signals.as_factors(),
            updated_at=None,
        )
    return ReputationOut(
        provider_id=provider_id,
        score=f"{row.score:.4f}",
        completed_count=row.completed_count,
        failed_count=row.failed_count,
        verification_failures=row.verification_failures,
        disputes_lost=row.disputes_lost,
        persisted=True,
        factors=signals.as_factors(),
        updated_at=row.updated_at,
    )


@router.get("/provider/reputation", response_model=ReputationOut)
async def my_reputation(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReputationOut:
    """The calling provider's own reputation."""
    provider = await get_provider_by_user(db, current.id)
    if provider is None:
        raise _NOT_A_PROVIDER
    return await _shape(db, provider.id)


@router.get("/providers/{provider_id}/reputation", response_model=ReputationOut)
async def provider_reputation(
    provider_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReputationOut:
    provider = await _visible_provider(db, provider_id, current)
    return await _shape(db, provider.id)


@router.get(
    "/providers/{provider_id}/reputation/events",
    response_model=list[ReputationEventOut],
)
async def provider_reputation_events(
    provider_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ReputationEventOut]:
    """Append-only reputation history — the "why is my score X?" answer."""
    provider = await _visible_provider(db, provider_id, current)
    events = await reputation_svc.recent_events(db, provider.id, limit=limit)
    return [
        ReputationEventOut(
            event_id=e.id,
            kind=e.kind,
            delta=f"{e.delta:.4f}",
            reason=e.reason,
            job_id=e.job_id,
            machine_id=e.machine_id,
            created_at=e.created_at,
        )
        for e in events
    ]


@router.post(
    "/admin/providers/{provider_id}/slash",
    response_model=SlashOut,
    status_code=status.HTTP_201_CREATED,
)
async def slash_provider(
    provider_id: uuid.UUID,
    body: SlashCreate,
    request: Request,
    current: CurrentUser = Depends(require_roles("administrator")),
    db: AsyncSession = Depends(get_db),
) -> SlashOut:
    """Claw back provider earnings on PROVEN fraud (administrator only)."""
    amount_minor = ledger.to_minor(body.amount)
    try:
        event = await reputation_svc.slash_provider(
            db,
            provider_id=provider_id,
            amount_minor=amount_minor,
            actor_user_id=current.id,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
            job_id=body.job_id,
            ip=client_ip(request),
        )
    except reputation_svc.SlashRefused as exc:
        # A refused slash is itself security-relevant: record it, then surface a
        # precise code (404 only for a provider that does not exist at all).
        await record_audit(
            db,
            action="provider.slash",
            outcome="refused",
            actor_user_id=current.id,
            resource_type="provider",
            resource_id=str(provider_id),
            ip=client_ip(request),
            reason=f"{exc.code} (amount_minor={amount_minor})",
        )
        await db.commit()
        if exc.code == "provider_not_found":
            raise _PROVIDER_NOT_FOUND
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code)
    except ledger.InsufficientFunds as exc:
        await record_audit(
            db,
            action="provider.slash",
            outcome="refused",
            actor_user_id=current.id,
            resource_type="provider",
            resource_id=str(provider_id),
            ip=client_ip(request),
            reason=f"insufficient_earnings shortfall_minor={exc.shortfall_minor}",
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="insufficient_earnings"
        )
    await db.commit()
    await db.refresh(event)
    return SlashOut(
        slash_id=event.id,
        provider_id=event.provider_id,
        job_id=event.job_id,
        amount=ledger.minor_to_str(event.amount_minor),
        currency=event.currency,
        reason=event.reason,
        ledger_txn_id=event.ledger_txn_id,
        created_at=event.created_at,
    )
