"""Customer invoice endpoints (JWT, owner-scoped — others -> 404)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps import get_current_user
from app.models.billing import Invoice
from app.schemas.auth import CurrentUser
from app.schemas.billing import InvoiceOut, LineItem
from app.services import ledger

router = APIRouter(prefix="/v1/invoices", tags=["invoices"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")


def shape_invoice(inv: Invoice) -> InvoiceOut:
    return InvoiceOut(
        invoice_id=inv.id,
        job_id=inv.job_id,
        line_items=[LineItem(**li) for li in (inv.line_items or [])],
        estimated_total=ledger.minor_to_str(inv.estimated_total_minor),
        actual_total=ledger.minor_to_str(inv.actual_total_minor),
        platform_fee=ledger.minor_to_str(inv.platform_fee_minor),
        currency=inv.currency,
        status=inv.status,
        created_at=inv.created_at,
    )


async def _own_invoice(db: AsyncSession, customer_id: uuid.UUID, invoice_id: uuid.UUID) -> Invoice:
    inv = (
        await db.execute(
            select(Invoice).where(
                Invoice.id == invoice_id, Invoice.customer_id == customer_id
            )
        )
    ).scalar_one_or_none()
    if inv is None:
        raise _NOT_FOUND
    return inv


@router.get("", response_model=list[InvoiceOut])
async def list_invoices(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[InvoiceOut]:
    rows = (
        await db.execute(
            select(Invoice)
            .where(Invoice.customer_id == current.id)
            .order_by(Invoice.created_at.desc())
        )
    ).scalars().all()
    return [shape_invoice(i) for i in rows]


@router.get("/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InvoiceOut:
    return shape_invoice(await _own_invoice(db, current.id, invoice_id))


@router.get("/{invoice_id}/download", response_model=InvoiceOut)
async def download_invoice(
    invoice_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InvoiceOut:
    """Inline, owner-only invoice download (others -> 404)."""
    return shape_invoice(await _own_invoice(db, current.id, invoice_id))
