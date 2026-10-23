"""Read-only usage & cost analytics (Phase B4).

Every query is hard-scoped to ``UsageRecord.customer_id == caller.id`` — there
is no admin bypass and no "all customers" mode here, so a path/query parameter
can never widen the result set to another tenant. A ``project_id`` that the
caller does not own returns 404, never 403.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ensure_aware
from app.db.session import get_db
from app.deps import get_current_user
from app.models.billing import CURRENCY, BillingAccount, LedgerEntry, LedgerTransaction, UsageRecord
from app.models.jobs import Job
from app.models.org import Project
from app.schemas.analytics import USAGE_GROUPINGS, UsageBucket, UsageReport, UsageSummary
from app.schemas.auth import CurrentUser
from app.services.ledger import minor_to_str
from app.services.spend_limits import SPEND_TXN_KINDS

router = APIRouter(prefix="/analytics", tags=["analytics"])

CSV_COLUMNS = (
    "key",
    "label",
    "job_count",
    "actual_seconds",
    "estimated_cost",
    "actual_cost",
    "currency",
)


async def _assert_owns_project(
    db: AsyncSession, project_id: uuid.UUID, user: CurrentUser
) -> None:
    """404 (not 403) for a project the caller does not own."""
    owned = (
        await db.execute(
            select(Project.id).where(
                Project.id == project_id, Project.owner_id == user.id
            )
        )
    ).scalar_one_or_none()
    if owned is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )


async def _ledger_net_spend_minor(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    start: dt.datetime | None,
    end: dt.datetime | None,
) -> int:
    """Net customer outflow over the window, from the ledger (never a counter)."""
    account = (
        await db.execute(
            select(BillingAccount).where(
                BillingAccount.owner_type == "customer",
                BillingAccount.owner_id == customer_id,
                BillingAccount.type == "customer_credits",
            )
        )
    ).scalar_one_or_none()
    if account is None:
        return 0

    conditions = [
        LedgerEntry.account_id == account.id,
        LedgerTransaction.kind.in_(SPEND_TXN_KINDS),
    ]
    if start is not None:
        conditions.append(LedgerTransaction.created_at >= start)
    if end is not None:
        conditions.append(LedgerTransaction.created_at < end)

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
    return totals.get("debit", 0) - totals.get("credit", 0)


async def _build_report(
    db: AsyncSession,
    *,
    user: CurrentUser,
    group_by: str,
    project_id: uuid.UUID | None,
    start: dt.datetime | None,
    end: dt.datetime | None,
) -> UsageReport:
    start = ensure_aware(start)
    end = ensure_aware(end)
    if start is not None and end is not None and end <= start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end must be after start",
        )
    if project_id is not None:
        await _assert_owns_project(db, project_id, user)

    conditions = [UsageRecord.customer_id == user.id]
    if start is not None:
        conditions.append(UsageRecord.created_at >= start)
    if end is not None:
        conditions.append(UsageRecord.created_at < end)
    if project_id is not None:
        conditions.append(Job.project_id == project_id)

    # Join Job for the project/model dimensions; it is an inner join because a
    # usage record without its job is not attributable to anything.
    base = select(UsageRecord, Job).join(Job, Job.id == UsageRecord.job_id).where(*conditions)
    rows = (await db.execute(base)).all()

    totals = {
        "job_count": 0,
        "actual_seconds": 0,
        "estimated_cost_minor": 0,
        "actual_cost_minor": 0,
    }
    buckets: dict[str, dict] = {}

    for usage, job in rows:
        if group_by == "project":
            key = str(job.project_id) if job.project_id else "unassigned"
        elif group_by == "model":
            key = job.model
        elif group_by == "day":
            created = ensure_aware(usage.created_at)
            key = created.date().isoformat()
        else:  # total
            key = "total"

        bucket = buckets.setdefault(
            key,
            {
                "key": key,
                "label": None,
                "job_count": 0,
                "actual_seconds": 0,
                "estimated_cost_minor": 0,
                "actual_cost_minor": 0,
            },
        )
        bucket["job_count"] += 1
        bucket["actual_seconds"] += int(usage.actual_seconds)
        bucket["estimated_cost_minor"] += int(usage.estimated_cost_minor)
        bucket["actual_cost_minor"] += int(usage.actual_cost_minor)

        totals["job_count"] += 1
        totals["actual_seconds"] += int(usage.actual_seconds)
        totals["estimated_cost_minor"] += int(usage.estimated_cost_minor)
        totals["actual_cost_minor"] += int(usage.actual_cost_minor)

    if group_by == "project":
        project_ids = [
            uuid.UUID(key) for key in buckets if key != "unassigned"
        ]
        if project_ids:
            named = (
                await db.execute(
                    select(Project.id, Project.name).where(
                        Project.id.in_(project_ids), Project.owner_id == user.id
                    )
                )
            ).all()
            for pid, name in named:
                buckets[str(pid)]["label"] = name

    net_spend = await _ledger_net_spend_minor(
        db, customer_id=user.id, start=start, end=end
    )

    return UsageReport(
        customer_id=user.id,
        project_id=project_id,
        group_by=group_by,
        start=start,
        end=end,
        currency=CURRENCY,
        summary=UsageSummary(
            job_count=totals["job_count"],
            actual_seconds=totals["actual_seconds"],
            estimated_cost_minor=totals["estimated_cost_minor"],
            estimated_cost=minor_to_str(totals["estimated_cost_minor"]),
            actual_cost_minor=totals["actual_cost_minor"],
            actual_cost=minor_to_str(totals["actual_cost_minor"]),
            ledger_net_spend_minor=net_spend,
            ledger_net_spend=minor_to_str(net_spend),
        ),
        buckets=[
            UsageBucket(
                key=item["key"],
                label=item["label"],
                job_count=item["job_count"],
                actual_seconds=item["actual_seconds"],
                estimated_cost_minor=item["estimated_cost_minor"],
                estimated_cost=minor_to_str(item["estimated_cost_minor"]),
                actual_cost_minor=item["actual_cost_minor"],
                actual_cost=minor_to_str(item["actual_cost_minor"]),
            )
            for item in sorted(buckets.values(), key=lambda b: b["key"])
        ],
    )


def _csv_safe(value: object) -> str:
    """Neutralize CSV-injection: a cell starting with = + - @ is executed as a
    formula when the export is opened in a spreadsheet, and project/model names
    are customer-supplied. Prefixing an apostrophe keeps the text readable."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _validate_group_by(group_by: str) -> str:
    if group_by not in USAGE_GROUPINGS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"group_by must be one of {USAGE_GROUPINGS}",
        )
    return group_by


@router.get("/usage", response_model=UsageReport)
async def get_usage(
    group_by: str = Query(default="total"),
    project_id: uuid.UUID | None = Query(default=None),
    start: dt.datetime | None = Query(default=None),
    end: dt.datetime | None = Query(default=None),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UsageReport:
    """Metered usage and cost for the caller, grouped and date-range filtered."""
    return await _build_report(
        db,
        user=current,
        group_by=_validate_group_by(group_by),
        project_id=project_id,
        start=start,
        end=end,
    )


@router.get("/usage.csv", response_class=Response)
async def export_usage_csv(
    group_by: str = Query(default="total"),
    project_id: uuid.UUID | None = Query(default=None),
    start: dt.datetime | None = Query(default=None),
    end: dt.datetime | None = Query(default=None),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The same report as a CSV download. Money stays a decimal string."""
    report = await _build_report(
        db,
        user=current,
        group_by=_validate_group_by(group_by),
        project_id=project_id,
        start=start,
        end=end,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for bucket in report.buckets:
        writer.writerow(
            [
                _csv_safe(bucket.key),
                _csv_safe(bucket.label or ""),
                bucket.job_count,
                bucket.actual_seconds,
                bucket.estimated_cost,
                bucket.actual_cost,
                report.currency,
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="computecoin-usage.csv"',
            # The export is per-customer data: never let a shared cache hold it.
            "Cache-Control": "no-store",
        },
    )
