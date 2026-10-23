"""Spend limit (budget) CRUD — owner-scoped, ledger-backed (Phase B4).

Every resource-by-id route resolves through :func:`_get_owned_limit`, which
returns 404 (never 403) for someone else's budget so the API never confirms that
another customer's limit id exists.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.session import get_db
from app.deps import client_ip, get_current_user, require_verified_user
from app.models.org import Project
from app.models.spend import SPEND_PERIODS, SpendAlert, SpendLimit
from app.schemas.auth import CurrentUser
from app.services import spend_limits as spend_service
from app.services.audit import record_audit

router = APIRouter(prefix="/spend-limits", tags=["spend-limits"])

_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Spend limit not found"
)

# api_key scope is accepted by the data model but NOT offered by the API: no
# column anywhere links a job (and therefore a ledger transaction) to the API
# key that created it, so an api_key-scoped cap could never be enforced. A limit
# that silently does not limit is worse than no limit, so it is refused.
SUPPORTED_SCOPES = ("customer", "project")


class SpendLimitCreate(BaseModel):
    scope: str = Field(description="customer | project")
    scope_id: uuid.UUID | None = Field(
        default=None, description="Project id for scope=project; ignored for customer."
    )
    period: str = Field(description="daily | monthly | total")
    limit_minor: int = Field(gt=0, le=10**15, description="Cap in integer minor units.")
    alert_thresholds: list[int] = Field(default_factory=list)
    enforce: bool = True

    @field_validator("scope")
    @classmethod
    def _check_scope(cls, value: str) -> str:
        if value not in SUPPORTED_SCOPES:
            raise ValueError(f"scope must be one of {SUPPORTED_SCOPES}")
        return value

    @field_validator("period")
    @classmethod
    def _check_period(cls, value: str) -> str:
        if value not in SPEND_PERIODS:
            raise ValueError(f"period must be one of {SPEND_PERIODS}")
        return value

    @field_validator("alert_thresholds")
    @classmethod
    def _check_thresholds(cls, value: list[int]) -> list[int]:
        for pct in value:
            if not 0 < int(pct) <= 1000:
                raise ValueError("alert thresholds must be between 1 and 1000 percent")
        return sorted({int(pct) for pct in value})


class SpendLimitUpdate(BaseModel):
    limit_minor: int | None = Field(default=None, gt=0, le=10**15)
    alert_thresholds: list[int] | None = None
    enforce: bool | None = None
    active: bool | None = None


class SpendLimitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scope: str
    scope_id: uuid.UUID
    owner_id: uuid.UUID
    period: str
    limit_minor: int
    currency: str
    alert_thresholds: list[int]
    enforce: bool
    active: bool
    created_at: dt.datetime


class SpendLimitUsageOut(BaseModel):
    period_key: str
    spent_minor: int
    spent: str
    limit_minor: int
    limit: str
    remaining_minor: int
    remaining: str
    used_pct: int


class SpendAlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    spend_limit_id: uuid.UUID
    threshold_pct: int
    period_key: str
    spent_minor: int
    limit_minor: int
    sent_at: dt.datetime


async def _resolve_scope_id(
    db: AsyncSession, body: SpendLimitCreate, user: CurrentUser
) -> uuid.UUID:
    """Authorize the target of the limit and return its id.

    A customer may only budget themselves or a project they own — otherwise a
    caller could create limits against another tenant's project ids and probe
    for their existence.
    """
    if body.scope == "customer":
        if body.scope_id is not None and body.scope_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="customer-scoped limits may only target yourself",
            )
        return user.id

    if body.scope_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="scope_id is required for scope=project",
        )
    project = (
        await db.execute(
            select(Project).where(
                Project.id == body.scope_id, Project.owner_id == user.id
            )
        )
    ).scalar_one_or_none()
    if project is None:
        # 404, not 403: never confirm another owner's project id.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return project.id


async def _get_owned_limit(
    db: AsyncSession, limit_id: uuid.UUID, user: CurrentUser
) -> SpendLimit:
    limit = (
        await db.execute(select(SpendLimit).where(SpendLimit.id == limit_id))
    ).scalar_one_or_none()
    if limit is None or limit.owner_id != user.id:
        raise _NOT_FOUND
    return limit


@router.post("", response_model=SpendLimitOut, status_code=status.HTTP_201_CREATED)
async def create_spend_limit(
    body: SpendLimitCreate,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> SpendLimitOut:
    """Create a budget cap. One limit per (scope, scope_id, period)."""
    scope_id = await _resolve_scope_id(db, body, current)
    limit = SpendLimit(
        scope=body.scope,
        scope_id=scope_id,
        owner_id=current.id,
        period=body.period,
        limit_minor=int(body.limit_minor),
        alert_thresholds=body.alert_thresholds,
        enforce=body.enforce,
        active=True,
    )
    try:
        async with db.begin_nested():
            db.add(limit)
            await db.flush()
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A spend limit already exists for that scope and period",
        )
    await record_audit(
        db,
        action="spend_limit.create",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="spend_limit",
        resource_id=str(limit.id),
        reason=f"{body.scope}/{body.period}",
    )
    return SpendLimitOut.model_validate(limit)


@router.get("", response_model=list[SpendLimitOut])
async def list_spend_limits(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SpendLimitOut]:
    rows = (
        await db.execute(
            select(SpendLimit)
            .where(SpendLimit.owner_id == current.id)
            .order_by(SpendLimit.created_at)
        )
    ).scalars().all()
    return [SpendLimitOut.model_validate(row) for row in rows]


@router.get("/{limit_id}", response_model=SpendLimitOut)
async def get_spend_limit(
    limit_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SpendLimitOut:
    return SpendLimitOut.model_validate(await _get_owned_limit(db, limit_id, current))


@router.get("/{limit_id}/usage", response_model=SpendLimitUsageOut)
async def get_spend_limit_usage(
    limit_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SpendLimitUsageOut:
    """Current-period spend for this budget, computed from the ledger."""
    limit = await _get_owned_limit(db, limit_id, current)
    return SpendLimitUsageOut(**await spend_service.limit_usage(db, limit))


@router.get("/{limit_id}/alerts", response_model=list[SpendAlertOut])
async def list_spend_alerts(
    limit_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SpendAlertOut]:
    limit = await _get_owned_limit(db, limit_id, current)
    rows = (
        await db.execute(
            select(SpendAlert)
            .where(SpendAlert.spend_limit_id == limit.id)
            .order_by(SpendAlert.sent_at)
        )
    ).scalars().all()
    return [SpendAlertOut.model_validate(row) for row in rows]


@router.patch("/{limit_id}", response_model=SpendLimitOut)
async def update_spend_limit(
    limit_id: uuid.UUID,
    body: SpendLimitUpdate,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> SpendLimitOut:
    limit = await _get_owned_limit(db, limit_id, current)
    if body.limit_minor is not None:
        limit.limit_minor = int(body.limit_minor)
    if body.alert_thresholds is not None:
        for pct in body.alert_thresholds:
            if not 0 < int(pct) <= 1000:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="alert thresholds must be between 1 and 1000 percent",
                )
        limit.alert_thresholds = sorted({int(pct) for pct in body.alert_thresholds})
    if body.enforce is not None:
        limit.enforce = body.enforce
    if body.active is not None:
        limit.active = body.active
    limit.updated_at = utcnow()
    await db.flush()
    await record_audit(
        db,
        action="spend_limit.update",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="spend_limit",
        resource_id=str(limit.id),
    )
    return SpendLimitOut.model_validate(limit)


@router.delete(
    "/{limit_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
async def delete_spend_limit(
    limit_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    limit = await _get_owned_limit(db, limit_id, current)
    await db.delete(limit)
    await db.flush()
    await record_audit(
        db,
        action="spend_limit.delete",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="spend_limit",
        resource_id=str(limit_id),
    )
