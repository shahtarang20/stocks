"""The withdrawal kill switch, second half: the admin / automatic pause (B1).

``WITHDRAWALS_ENABLED`` (a setting, default off) is the first half. This is the second: a flag in
the database that an admin sets, and that reconciliation sets by itself on a mismatch. While paused
no new request is accepted and the worker sends nothing; it only keeps watching what is in flight.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.system import SystemConfiguration

PAUSE_KEY = "withdrawals_pause"


async def get_pause(db: AsyncSession) -> dict[str, Any]:
    row = await db.get(SystemConfiguration, PAUSE_KEY)
    if row is None:
        return {"paused": False}
    return dict(row.value or {"paused": False})


async def is_paused(db: AsyncSession) -> bool:
    return bool((await get_pause(db)).get("paused"))


async def set_pause(db: AsyncSession, paused: bool, *, reason: str, source: str) -> dict[str, Any]:
    """Pause or resume. ``source`` is 'admin' or 'reconciliation'. The caller commits."""
    value = {
        "paused": bool(paused),
        "reason": reason[:500],
        "source": source,
        "at": utcnow().isoformat(),
    }
    row = await db.get(SystemConfiguration, PAUSE_KEY)
    if row is None:
        db.add(SystemConfiguration(key=PAUSE_KEY, value=value))
    else:
        row.value = value
    await db.flush()
    return value
