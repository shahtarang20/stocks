"""Phase D2 — admin API for on-chain <-> off-chain reconciliation runs.

Administrator-only: a reconciliation report names escrows, job ids and amounts
across every tenant, so it is platform-operator data, not customer data.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps import client_ip, require_roles
from app.models.reconciliation import ChainReconciliationRun
from app.schemas.auth import CurrentUser
from app.services.audit import record_audit
from app.services.chain_reconciler import (
    ChainClient,
    ChainClientError,
    ChainReconciler,
    build_default_client,
    run_to_dict,
)

router = APIRouter(prefix="/v1/chain/reconciliation", tags=["chain-reconciliation"])

#: Test/integration seam. When set, this client is used instead of the JSON-RPC
#: one, so the endpoints can be exercised offline without monkeypatching httpx.
_client_override: ChainClient | None = None


def set_chain_client(client: ChainClient | None) -> None:
    """Install (or clear) the injected chain client used by the endpoints."""
    global _client_override
    _client_override = client


def get_chain_client() -> ChainClient:
    """Resolve the chain client, preferring an explicitly injected one."""
    if _client_override is not None:
        return _client_override
    return build_default_client()


def _require_enabled() -> None:
    """Reconciliation talks to an external chain; it stays off unless enabled.

    Read at request time (not import time) so operators can flip the flag
    without a redeploy and so tests can toggle it per-case.
    """
    from app.core.config import settings

    if not settings.CHAIN_RECONCILE_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="chain_reconciliation_disabled",
        )


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def trigger_reconciliation_run(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_roles("administrator")),
) -> dict[str, Any]:
    """Run one reconciliation sweep and return its persisted result.

    The response's ``ok`` comes from the comparison itself, never from the
    feature flag: an RPC failure yields ``ok=false`` with ``error_count>0``.
    """
    _require_enabled()
    try:
        client = get_chain_client()
    except ChainClientError as exc:
        # Misconfiguration is an operator error, not a silent pass.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"chain_client_unavailable:{exc}",
        ) from exc

    run = await ChainReconciler(client).run(db, idempotency_key=idempotency_key)
    await record_audit(
        db,
        action="chain.reconciliation.run",
        outcome="ok" if run.ok else "drift",
        actor_user_id=current_user.id,
        resource_type="chain_reconciliation_run",
        resource_id=str(run.id),
        ip=client_ip(request),
        reason=f"checked={run.escrows_checked} drift={run.drift_count}",
    )
    return run_to_dict(run, include_detail=True)


@router.get("/runs")
async def list_reconciliation_runs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_roles("administrator")),
) -> list[dict[str, Any]]:
    """List past runs, newest first (summary only — detail can be large)."""
    _require_enabled()
    rows = (
        await db.scalars(
            select(ChainReconciliationRun)
            .order_by(desc(ChainReconciliationRun.started_at))
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [run_to_dict(r) for r in rows]


@router.get("/runs/{run_id}")
async def get_reconciliation_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_roles("administrator")),
) -> dict[str, Any]:
    """Return one run including its per-escrow findings."""
    _require_enabled()
    run = await db.get(ChainReconciliationRun, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="run_not_found"
        )
    return run_to_dict(run, include_detail=True)
