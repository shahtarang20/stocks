"""Fail-safe side-effect hooks.

Phase B/C/D added several *observational* side effects to the hot paths: result
verification evidence, reputation folding, webhook fan-out. None of them is
allowed to break the thing it observes — a webhook sink being unreachable, or a
reputation row failing to write, must never fail a job result, strand a lease or
cause a double charge.

:func:`safe_hook` runs such a block inside a SAVEPOINT. On success the savepoint
is released and the writes join the caller's transaction; on ANY exception only
the hook's own writes are rolled back and the session stays usable, so the
surrounding money/FSM work still commits. That is the key difference from a bare
``try/except``: a failed statement poisons the transaction until it is rolled
back, and a savepoint is the only way to undo just the hook.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("computecoin.hooks")


@asynccontextmanager
async def safe_hook(db: AsyncSession, name: str) -> AsyncIterator[None]:
    """Run a non-critical side effect so its failure cannot affect the caller."""
    savepoint = await db.begin_nested()
    try:
        yield
        if savepoint.is_active:
            await savepoint.commit()
    except Exception:  # noqa: BLE001 - deliberate: hooks are never fatal
        logger.exception("hook %s failed; continuing", name)
        if savepoint.is_active:
            try:
                await savepoint.rollback()
            except Exception:  # pragma: no cover - defensive
                logger.exception("hook %s rollback failed", name)
