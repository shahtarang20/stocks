"""Audit logging helper.

Writes audit_logs rows. Never logs secrets (callers must pass only
non-sensitive identifiers / reasons).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system import AuditLog


async def record_audit(
    db: AsyncSession,
    *,
    action: str,
    outcome: str,
    actor_user_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    ip: str | None = None,
    reason: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_user_id=actor_user_id,
        action=action,
        outcome=outcome,
        resource_type=resource_type,
        resource_id=resource_id,
        ip=ip,
        reason=reason,
    )
    db.add(entry)
    await db.flush()
    return entry
