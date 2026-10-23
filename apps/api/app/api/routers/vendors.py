"""Admin: third-party vendor services, quotas and renewal reminders.

Administrator-only throughout. These rows describe the platform's own
infrastructure spend and its vendor dashboards — not customer data — so they
are never exposed to a customer or a provider.
"""

from __future__ import annotations

import decimal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.session import get_db
from app.deps import require_roles
from app.models.vendors import NO_THRESHOLD, VendorAlert, VendorService
from app.schemas.auth import CurrentUser
from app.schemas.vendors import (
    VendorAlertOut,
    VendorCardOut,
    VendorSummaryOut,
    VendorUpdate,
    VendorUsageIn,
)
from app.services import vendors as vendor_service
from app.services.audit import record_audit

router = APIRouter(prefix="/v1/admin/vendors", tags=["admin-vendors"])


def _num(v: decimal.Decimal | None) -> str | None:
    if v is None:
        return None
    d = decimal.Decimal(v)
    return str(int(d)) if d == d.to_integral_value() else f"{d:.2f}"


def _alert_out(a: VendorAlert, vendor_name: str | None) -> VendorAlertOut:
    """Map the NO_THRESHOLD sentinel back to null for API consumers."""
    return VendorAlertOut(
        id=a.id,
        vendor_id=a.vendor_id,
        vendor_name=vendor_name,
        alert_type=a.alert_type,
        threshold_pct=None if a.threshold_pct == NO_THRESHOLD else a.threshold_pct,
        period_key=a.period_key,
        message=a.message,
        acknowledged=a.acknowledged,
        created_at=a.created_at,
    )


def _card(v: VendorService) -> VendorCardOut:
    st = vendor_service.evaluate(v)
    return VendorCardOut(
        id=v.id,
        key=v.key,
        name=v.name,
        category=v.category,
        purpose=v.purpose,
        dashboard_url=v.dashboard_url,
        plan_name=v.plan_name,
        quota_limit=_num(v.quota_limit),
        quota_unit=v.quota_unit,
        quota_period=v.quota_period,
        current_usage=_num(v.current_usage),
        usage_source=v.usage_source,
        usage_updated_at=v.usage_updated_at,
        cost_amount=(
            f"{decimal.Decimal(v.cost_amount).quantize(decimal.Decimal('0.01'))}"
            if v.cost_amount is not None
            else None
        ),
        cost_currency=v.cost_currency,
        billing_cycle=v.billing_cycle,
        renewal_date=v.renewal_date,
        reminder_days_before=v.reminder_days_before,
        usage_pct=st.usage_pct,
        status=st.status,
        detail=st.detail,
        days_until_renewal=st.days_until_renewal,
        renewal_due=st.renewal_due,
        measurable=st.measurable,
        notes=v.notes,
        active=v.active,
    )


@router.get("", response_model=list[VendorCardOut])
async def list_vendors(
    include_inactive: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_roles("administrator")),
):
    """Every third-party dependency, worst status first."""
    stmt = select(VendorService)
    if not include_inactive:
        stmt = stmt.where(VendorService.active.is_(True))
    rows = (await db.scalars(stmt)).all()
    cards = [_card(v) for v in rows]
    order = {"critical": 0, "warning": 1, "unknown": 2, "watch": 3, "ok": 4}
    cards.sort(key=lambda c: (order.get(c.status, 9), c.name))
    return cards


@router.get("/summary", response_model=VendorSummaryOut)
async def vendor_summary(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_roles("administrator")),
):
    """Headline counts. The cost total covers ONLY services with a cost set."""
    rows = (
        await db.scalars(select(VendorService).where(VendorService.active.is_(True)))
    ).all()
    cards = [_card(v) for v in rows]

    monthly = decimal.Decimal("0")
    missing = 0
    for v in rows:
        if v.cost_amount is None:
            missing += 1
            continue
        amount = decimal.Decimal(v.cost_amount)
        # Normalise to a monthly figure so the total is comparable.
        if v.billing_cycle == "annual":
            amount = amount / 12
        monthly += amount

    unacked = await db.scalar(
        select(VendorAlert).where(VendorAlert.acknowledged.is_(False)).limit(1)
    )
    unacked_count = len(
        (
            await db.scalars(
                select(VendorAlert).where(VendorAlert.acknowledged.is_(False))
            )
        ).all()
    ) if unacked is not None else 0

    return VendorSummaryOut(
        total=len(cards),
        critical=sum(1 for c in cards if c.status == "critical"),
        warning=sum(1 for c in cards if c.status == "warning"),
        unknown=sum(1 for c in cards if c.status == "unknown"),
        renewals_due=sum(1 for c in cards if c.renewal_due),
        unacknowledged_alerts=unacked_count,
        known_monthly_cost=f"{monthly.quantize(decimal.Decimal('0.01'))}",
        cost_currency="USD",
        services_without_cost=missing,
    )


@router.patch("/{vendor_id}", response_model=VendorCardOut)
async def update_vendor(
    vendor_id: uuid.UUID,
    body: VendorUpdate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_roles("administrator")),
):
    """Set the plan, cost, quota or renewal date for one service."""
    vendor = await vendor_service.get_vendor(db, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="vendor_not_found")

    changed: dict[str, str] = {}
    for field in (
        "plan_name", "quota_limit", "quota_unit", "quota_period", "cost_amount",
        "cost_currency", "billing_cycle", "renewal_date", "reminder_days_before",
        "alert_thresholds", "notes", "active",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(vendor, field, value)
            changed[field] = str(value)

    vendor.updated_at = utcnow()
    await record_audit(
        db,
        action="admin.vendor.update",
        outcome="success",
        actor_user_id=current.id,
        resource_type="vendor_service",
        resource_id=str(vendor.id),
        reason=f"updated {', '.join(sorted(changed)) or 'nothing'}",
    )
    await db.commit()
    await db.refresh(vendor)
    return _card(vendor)


@router.post("/{vendor_id}/usage", response_model=VendorCardOut)
async def set_usage(
    vendor_id: uuid.UUID,
    body: VendorUsageIn,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_roles("administrator")),
):
    """Record a usage reading, stamping where the number came from."""
    vendor = await vendor_service.get_vendor(db, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="vendor_not_found")
    await vendor_service.record_usage(db, vendor.key, body.value, source=body.source)
    await vendor_service.evaluate_and_alert(db)
    await db.commit()
    await db.refresh(vendor)
    return _card(vendor)


@router.post("/evaluate", response_model=list[VendorAlertOut])
async def evaluate_now(
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_roles("administrator")),
):
    """Run the sweep now. Returns only alerts that had not already fired."""
    fired = await vendor_service.evaluate_and_alert(db)
    await db.commit()
    names = {
        v.id: v.name
        for v in (await db.scalars(select(VendorService))).all()
    }
    return [_alert_out(a, names.get(a.vendor_id)) for a in fired]


@router.get("/alerts", response_model=list[VendorAlertOut])
async def list_alerts(
    unacknowledged_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_roles("administrator")),
):
    stmt = select(VendorAlert).order_by(desc(VendorAlert.created_at)).limit(limit)
    if unacknowledged_only:
        stmt = stmt.where(VendorAlert.acknowledged.is_(False))
    rows = (await db.scalars(stmt)).all()
    names = {
        v.id: v.name for v in (await db.scalars(select(VendorService))).all()
    }
    return [_alert_out(a, names.get(a.vendor_id)) for a in rows]


@router.post("/alerts/{alert_id}/acknowledge", response_model=VendorAlertOut)
async def acknowledge_alert(
    alert_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_roles("administrator")),
):
    """Acknowledging hides an alert. It does NOT re-arm it: the alert still
    counts as fired for its period, so the same crossing never repeats."""
    alert = await db.scalar(select(VendorAlert).where(VendorAlert.id == alert_id))
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alert_not_found")
    alert.acknowledged = True
    await db.commit()
    await db.refresh(alert)
    vendor = await vendor_service.get_vendor(db, alert.vendor_id)
    return _alert_out(alert, vendor.name if vendor else None)
