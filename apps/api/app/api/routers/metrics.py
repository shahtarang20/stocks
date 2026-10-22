"""Prometheus-format operational metrics (quick-win observability).

Exposes job-state counts, fleet/online machines, active reservations, total
users, and — most importantly for a money platform — the live ledger imbalance
(debits - credits, which must always be 0). No PII or secrets are exposed.

Access: open when METRICS_TOKEN is unset (dev). When METRICS_TOKEN is set, a
`Authorization: Bearer <METRICS_TOKEN>` header is required (set it in prod and
scrape over a private network).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models.billing import LedgerEntry
from app.models.jobs import Job
from app.models.marketplace import ResourceReservation
from app.models.provider import ProviderMachine
from app.models.user import User

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if settings.METRICS_TOKEN:
        expected = f"Bearer {settings.METRICS_TOKEN}"
        if authorization != expected:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="metrics_unauthorized")

    lines: list[str] = []

    # Jobs by status.
    lines += ["# HELP computecoin_jobs_total Jobs by FSM status.",
              "# TYPE computecoin_jobs_total gauge"]
    for job_status, cnt in (await db.execute(select(Job.status, func.count()).group_by(Job.status))).all():
        lines.append(f'computecoin_jobs_total{{status="{job_status}"}} {int(cnt)}')

    # Online machines.
    online = (await db.execute(
        select(func.count()).select_from(ProviderMachine).where(ProviderMachine.availability == "online")
    )).scalar() or 0
    lines += ["# TYPE computecoin_machines_online gauge", f"computecoin_machines_online {int(online)}"]

    # Active reservations.
    resv = (await db.execute(
        select(func.count()).select_from(ResourceReservation).where(ResourceReservation.status == "active")
    )).scalar() or 0
    lines += ["# TYPE computecoin_reservations_active gauge", f"computecoin_reservations_active {int(resv)}"]

    # Total users.
    users = (await db.execute(select(func.count()).select_from(User))).scalar() or 0
    lines += ["# TYPE computecoin_users_total gauge", f"computecoin_users_total {int(users)}"]

    # Ledger imbalance (must be 0) — alert on drift.
    debits = (await db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(LedgerEntry.direction == "debit")
    )).scalar() or 0
    credits = (await db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(LedgerEntry.direction == "credit")
    )).scalar() or 0
    lines += ["# HELP computecoin_ledger_imbalance_minor Ledger debits minus credits in minor units; MUST be 0.",
              "# TYPE computecoin_ledger_imbalance_minor gauge",
              f"computecoin_ledger_imbalance_minor {int(debits) - int(credits)}"]

    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
