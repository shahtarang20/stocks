"""Customer reservation endpoints (customer JWT, owner-scoped)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.session import get_db
from app.deps import client_ip, get_current_user
from app.models.marketplace import ResourceReservation
from app.schemas.auth import CurrentUser
from app.schemas.marketplace import ReservationOut, ReservationReleaseOut
from app.services.audit import record_audit
from app.services.marketplace import derive_reservation_status

router = APIRouter(prefix="/v1/reservations", tags=["reservations"])

_RESERVATION_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Reservation not found"
)


async def _get_own_reservation(
    db: AsyncSession, customer_id: uuid.UUID, reservation_id: uuid.UUID
) -> ResourceReservation | None:
    result = await db.execute(
        select(ResourceReservation).where(
            ResourceReservation.id == reservation_id,
            ResourceReservation.customer_id == customer_id,
        )
    )
    return result.scalar_one_or_none()


@router.get("", response_model=list[ReservationOut])
async def list_reservations(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ReservationOut]:
    result = await db.execute(
        select(ResourceReservation)
        .where(ResourceReservation.customer_id == current.id)
        .order_by(ResourceReservation.created_at.desc())
    )
    now = utcnow()
    return [
        ReservationOut(
            reservation_id=r.id,
            quote_id=r.quote_id,
            machine_id=r.machine_id,
            slots=r.slots,
            status=derive_reservation_status(r, now),
            expires_at=r.expires_at,
            created_at=r.created_at,
        )
        for r in result.scalars().all()
    ]


@router.post("/{reservation_id}/release", response_model=ReservationReleaseOut)
async def release_reservation(
    reservation_id: uuid.UUID,
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReservationReleaseOut:
    reservation = await _get_own_reservation(db, current.id, reservation_id)
    if reservation is None:
        raise _RESERVATION_NOT_FOUND

    # Idempotent: releasing an already-released reservation succeeds (no error).
    if reservation.status == "active":
        reservation.status = "released"
        reservation.released_at = utcnow()
        await record_audit(
            db,
            action="reservation.release",
            outcome="success",
            actor_user_id=current.id,
            ip=client_ip(request),
            resource_type="reservation",
            resource_id=str(reservation.id),
        )
        await db.flush()

    return ReservationReleaseOut(reservation_id=reservation.id, status="released")
