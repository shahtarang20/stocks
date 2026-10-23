"""Provider pricing & availability controls (Phase C3).

Owner-scoped machine policy: a hard price floor, a recurring availability
schedule and a model allowlist. Every endpoint resolves the machine through the
caller's own provider row and returns 404 (never 403) for someone else's
machine, so the API does not leak the existence of other operators' hardware.

The rules themselves live in :mod:`app.services.provider_pricing` so the
scheduler evaluates exactly what this router writes.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.session import get_db
from app.deps import client_ip, get_current_user
from app.models.provider import ProviderMachine
from app.schemas.auth import CurrentUser
from app.services.audit import record_audit
from app.services.provider import get_owned_machine, get_provider_by_user
from app.services.provider_pricing import (
    PricingPolicyError,
    machine_ineligibility_reason,
    price_respects_floor,
    validate_availability_windows,
    validate_floor_price,
    validate_model_allowlist,
)

router = APIRouter(prefix="/v1/provider", tags=["provider-pricing"])

_MACHINE_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found"
)


class PricingPolicyUpdate(BaseModel):
    """Partial update. An omitted field is left alone; an explicit ``null`` clears it."""

    asking_price_per_hour: decimal.Decimal | None = None
    floor_price_per_hour: decimal.Decimal | None = None
    availability_windows: list[dict] | None = None
    model_allowlist: list[str] | None = None


class PricingPolicyOut(BaseModel):
    machine_id: uuid.UUID
    asking_price_per_hour: str | None
    floor_price_per_hour: str | None
    availability_windows: list[dict] | None
    model_allowlist: list[str] | None
    eligible_now: bool
    ineligible_reason: str | None = None
    evaluated_at: dt.datetime = Field(default_factory=utcnow)


def _price_str(value: decimal.Decimal | None) -> str | None:
    return None if value is None else f"{decimal.Decimal(value):.2f}"


def _shape(machine: ProviderMachine, model: str | None = None) -> PricingPolicyOut:
    reason = machine_ineligibility_reason(machine, model)
    return PricingPolicyOut(
        machine_id=machine.id,
        asking_price_per_hour=_price_str(machine.asking_price_per_hour),
        floor_price_per_hour=_price_str(machine.floor_price_per_hour),
        availability_windows=machine.availability_windows,
        model_allowlist=machine.model_allowlist,
        eligible_now=reason is None,
        ineligible_reason=reason,
    )


async def _owned_machine(
    db: AsyncSession, current: CurrentUser, machine_id: uuid.UUID
) -> ProviderMachine:
    """Resolve a machine the CALLER owns, or 404.

    404 (not 403) is deliberate: a non-owner must not be able to distinguish
    "exists but not yours" from "does not exist".
    """
    provider = await get_provider_by_user(db, current.id)
    if provider is None:
        raise _MACHINE_NOT_FOUND
    machine = await get_owned_machine(db, provider.id, machine_id)
    if machine is None:
        raise _MACHINE_NOT_FOUND
    return machine


@router.get("/machines/{machine_id}/pricing", response_model=PricingPolicyOut)
async def get_machine_pricing(
    machine_id: uuid.UUID,
    model: str | None = Query(default=None, max_length=128),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PricingPolicyOut:
    """Current policy plus a live eligibility verdict (optionally for ``model``)."""
    machine = await _owned_machine(db, current, machine_id)
    return _shape(machine, model)


@router.put("/machines/{machine_id}/pricing", response_model=PricingPolicyOut)
async def update_machine_pricing(
    machine_id: uuid.UUID,
    body: PricingPolicyUpdate,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PricingPolicyOut:
    """Set the machine's floor price, availability schedule and model allowlist.

    The floor is enforced AT WRITE TIME as well as at schedule time: a request
    that would leave the asking price below the floor is refused outright, so an
    operator cannot accidentally park a machine in a permanently ineligible
    state.
    """
    machine = await _owned_machine(db, current, machine_id)
    fields = body.model_fields_set

    new_asking = machine.asking_price_per_hour
    if "asking_price_per_hour" in fields:
        if body.asking_price_per_hour is None:
            new_asking = None
        else:
            asking = decimal.Decimal(body.asking_price_per_hour)
            if asking <= 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="asking_price_must_be_positive",
                )
            new_asking = asking.quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)

    new_floor = machine.floor_price_per_hour
    new_windows = machine.availability_windows
    new_allowlist = machine.model_allowlist
    try:
        if "floor_price_per_hour" in fields:
            new_floor = validate_floor_price(body.floor_price_per_hour)
        if "availability_windows" in fields:
            new_windows = validate_availability_windows(body.availability_windows)
        if "model_allowlist" in fields:
            new_allowlist = validate_model_allowlist(body.model_allowlist)
    except PricingPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.code
        ) from exc

    if new_floor is not None and not price_respects_floor(new_asking, new_floor):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="asking_price_below_floor",
        )

    machine.asking_price_per_hour = new_asking
    machine.floor_price_per_hour = new_floor
    machine.availability_windows = new_windows
    machine.model_allowlist = new_allowlist

    await record_audit(
        db,
        action="provider.machine.pricing.update",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="machine",
        resource_id=str(machine.id),
        reason=f"floor={_price_str(new_floor)} windows={len(new_windows or [])} models={len(new_allowlist or [])}",
    )
    await db.flush()
    await db.refresh(machine)
    return _shape(machine)
