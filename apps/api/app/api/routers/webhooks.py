"""Webhook endpoint management (Phase B4).

The signing secret is returned EXACTLY ONCE, at creation (and on an explicit
rotate); only a derivation of it is persisted, so there is no route that can
ever read it back. Every by-id route is owner-filtered and answers 404 for
someone else's endpoint.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.session import get_db
from app.deps import client_ip, get_current_user, require_verified_user
from app.models.webhooks import WebhookDelivery, WebhookEndpoint
from app.schemas.auth import CurrentUser
from app.services import webhooks as webhook_service
from app.services.audit import record_audit

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Webhook endpoint not found"
)

# A customer may not register an unbounded number of sinks; each one multiplies
# the platform's outbound request volume per event.
MAX_ENDPOINTS_PER_CUSTOMER = 20


class WebhookEndpointCreate(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    events: list[str] = Field(min_length=1)
    description: str | None = Field(default=None, max_length=255)

    @field_validator("events")
    @classmethod
    def _known_events(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(webhook_service.WEBHOOK_EVENTS))
        if unknown:
            raise ValueError(
                f"unknown event(s) {unknown}; supported: {list(webhook_service.WEBHOOK_EVENTS)}"
            )
        return sorted(set(value))


class WebhookEndpointUpdate(BaseModel):
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    events: list[str] | None = None
    description: str | None = Field(default=None, max_length=255)
    active: bool | None = None

    @field_validator("events")
    @classmethod
    def _known_events(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("events must not be empty")
        unknown = sorted(set(value) - set(webhook_service.WEBHOOK_EVENTS))
        if unknown:
            raise ValueError(f"unknown event(s) {unknown}")
        return sorted(set(value))


class WebhookEndpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID
    url: str
    description: str | None
    events: list[str]
    active: bool
    failure_count: int
    last_error: str | None
    created_at: dt.datetime


class WebhookEndpointCreatedOut(WebhookEndpointOut):
    """Creation response — the only place the raw secret is ever returned."""

    secret: str = Field(description="Shown ONCE. Store it; it cannot be retrieved again.")


class WebhookDeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    endpoint_id: uuid.UUID
    event_type: str
    delivery_key: str
    status: str
    attempts: int
    response_status: int | None
    last_error: str | None
    next_attempt_at: dt.datetime
    created_at: dt.datetime
    delivered_at: dt.datetime | None


async def _validated_url(url: str) -> str:
    """Run the SSRF guard, mapping a refusal to a 400 with a stable code."""
    try:
        await webhook_service.validate_webhook_url(url)
    except webhook_service.WebhookUrlRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"webhook_url_rejected:{exc.code}"
        )
    return url


async def _get_owned_endpoint(
    db: AsyncSession, endpoint_id: uuid.UUID, user: CurrentUser
) -> WebhookEndpoint:
    endpoint = (
        await db.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.id == endpoint_id)
        )
    ).scalar_one_or_none()
    if endpoint is None or endpoint.customer_id != user.id:
        raise _NOT_FOUND
    return endpoint


@router.get("/events", response_model=list[str])
async def list_supported_events(
    _current: CurrentUser = Depends(get_current_user),
) -> list[str]:
    return list(webhook_service.WEBHOOK_EVENTS)


@router.post(
    "/endpoints",
    response_model=WebhookEndpointCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_endpoint(
    body: WebhookEndpointCreate,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> WebhookEndpointCreatedOut:
    """Register a sink. Refuses URLs that resolve into private address space."""
    existing = (
        await db.execute(
            select(WebhookEndpoint.id).where(WebhookEndpoint.customer_id == current.id)
        )
    ).scalars().all()
    if len(existing) >= MAX_ENDPOINTS_PER_CUSTOMER:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="too_many_webhook_endpoints"
        )

    await _validated_url(body.url)
    secret = webhook_service.generate_secret()
    endpoint = WebhookEndpoint(
        customer_id=current.id,
        url=body.url,
        description=body.description,
        secret_hash=webhook_service.hash_secret(secret),
        events=body.events,
        active=True,
    )
    db.add(endpoint)
    await db.flush()
    await record_audit(
        db,
        action="webhook.endpoint.create",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="webhook_endpoint",
        resource_id=str(endpoint.id),
    )
    return WebhookEndpointCreatedOut(
        **WebhookEndpointOut.model_validate(endpoint).model_dump(), secret=secret
    )


@router.get("/endpoints", response_model=list[WebhookEndpointOut])
async def list_endpoints(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WebhookEndpointOut]:
    rows = (
        await db.execute(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.customer_id == current.id)
            .order_by(WebhookEndpoint.created_at)
        )
    ).scalars().all()
    return [WebhookEndpointOut.model_validate(row) for row in rows]


@router.get("/endpoints/{endpoint_id}", response_model=WebhookEndpointOut)
async def get_endpoint(
    endpoint_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WebhookEndpointOut:
    return WebhookEndpointOut.model_validate(
        await _get_owned_endpoint(db, endpoint_id, current)
    )


@router.patch("/endpoints/{endpoint_id}", response_model=WebhookEndpointOut)
async def update_endpoint(
    endpoint_id: uuid.UUID,
    body: WebhookEndpointUpdate,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> WebhookEndpointOut:
    endpoint = await _get_owned_endpoint(db, endpoint_id, current)
    if body.url is not None:
        await _validated_url(body.url)
        endpoint.url = body.url
        # A new destination earns a clean slate.
        endpoint.failure_count = 0
        endpoint.last_error = None
    if body.events is not None:
        endpoint.events = body.events
    if body.description is not None:
        endpoint.description = body.description
    if body.active is not None:
        endpoint.active = body.active
    endpoint.updated_at = utcnow()
    await db.flush()
    await record_audit(
        db,
        action="webhook.endpoint.update",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="webhook_endpoint",
        resource_id=str(endpoint.id),
    )
    return WebhookEndpointOut.model_validate(endpoint)


@router.post(
    "/endpoints/{endpoint_id}/rotate-secret", response_model=WebhookEndpointCreatedOut
)
async def rotate_secret(
    endpoint_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
) -> WebhookEndpointCreatedOut:
    """Issue a new signing secret. The previous one stops verifying immediately."""
    endpoint = await _get_owned_endpoint(db, endpoint_id, current)
    secret = webhook_service.generate_secret()
    endpoint.secret_hash = webhook_service.hash_secret(secret)
    endpoint.updated_at = utcnow()
    await db.flush()
    await record_audit(
        db,
        action="webhook.endpoint.rotate_secret",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="webhook_endpoint",
        resource_id=str(endpoint.id),
    )
    return WebhookEndpointCreatedOut(
        **WebhookEndpointOut.model_validate(endpoint).model_dump(), secret=secret
    )


@router.get(
    "/endpoints/{endpoint_id}/deliveries", response_model=list[WebhookDeliveryOut]
)
async def list_deliveries(
    endpoint_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WebhookDeliveryOut]:
    endpoint = await _get_owned_endpoint(db, endpoint_id, current)
    rows = (
        await db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.endpoint_id == endpoint.id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(200)
        )
    ).scalars().all()
    return [WebhookDeliveryOut.model_validate(row) for row in rows]


@router.delete(
    "/endpoints/{endpoint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_endpoint(
    endpoint_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    endpoint = await _get_owned_endpoint(db, endpoint_id, current)
    await db.delete(endpoint)
    await db.flush()
    await record_audit(
        db,
        action="webhook.endpoint.delete",
        outcome="success",
        actor_user_id=current.id,
        ip=client_ip(request),
        resource_type="webhook_endpoint",
        resource_id=str(endpoint_id),
    )
