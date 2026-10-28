"""Dedicated background worker process for ComputeCoin lease recovery and job scheduling.

Can be run as a separate service on Railway/Docker:
    python -m app.worker
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.session import get_sessionmaker
from app.services.leases import run_recovery_cycle
from app.services.log_anchor import run_anchor_cycle
from app.services.log_witness import run_witness_cycle
from app.services.token_deposits import scan_deposits

setup_logging()
logger = logging.getLogger("computecoin.worker")


async def sweep_once(sm: Any, timers: dict[str, float], now: float) -> None:
    """One worker iteration. Each part has its own try/except: a chain or witness outage never
    stops lease recovery, and a failing anchor or witness cycle waits a full interval."""
    try:
        async with sm() as db:
            recovered = await run_recovery_cycle(db)
            await db.commit()
            if any(recovered.values()):
                logger.info("Recovery sweep completed: %s", recovered)
    except Exception as exc:
        logger.exception("Worker recovery cycle encountered an error: %s", exc)

    if settings.token_deposits_enabled:
        try:
            async with sm() as db:
                result = await scan_deposits(db)
                if any(result.get(k) for k in ("credited", "unclaimed", "rejected")):
                    logger.info("Token deposit scan: %s", result)
        except Exception as exc:
            logger.exception("Token deposit scan failed (will retry next sweep): %s", exc)

    if settings.PRIVACY_ANCHOR_ENABLED:
        if now - timers["anchor"] >= max(5, settings.PRIVACY_ANCHOR_INTERVAL_SECONDS):
            # Set BEFORE the attempt: a failing chain waits a full interval instead of being
            # retried (and possibly sent another transaction) on every sweep.
            timers["anchor"] = now
            try:
                async with sm() as db:
                    anchor_res = await run_anchor_cycle(db)
                    if anchor_res and anchor_res.get("anchored"):
                        logger.info("Transparency log anchor: %s", anchor_res)
            except Exception as exc:
                logger.exception("Transparency log anchor cycle failed: %s", exc)

    if settings.PRIVACY_WITNESS_THRESHOLD > 0:
        if now - timers["witness"] >= max(5, settings.PRIVACY_WITNESS_INTERVAL_SECONDS):
            timers["witness"] = now
            try:
                async with sm() as db:
                    witness_res = await run_witness_cycle(db)
                    if witness_res and witness_res.get("witnessed"):
                        logger.info("Transparency log witnessed: %s", witness_res)
            except Exception as exc:
                logger.exception("Transparency log witness cycle failed: %s", exc)


async def main() -> None:
    logger.info(
        "Starting ComputeCoin Background Worker (env: %s, sweep interval: %ds)",
        settings.ENVIRONMENT,
        settings.SWEEP_INTERVAL_SECONDS,
    )

    if settings.SENTRY_DSN:
        try:
            import sentry_sdk
            sentry_sdk.init(
                dsn=settings.SENTRY_DSN,
                environment=settings.ENVIRONMENT,
                traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
                send_default_pii=False,
            )
            logger.info("Sentry monitoring enabled for worker")
        except Exception as exc:
            logger.warning("Could not initialize Sentry in worker: %s", exc)

    stop_event = asyncio.Event()

    def _handle_exit_signal(*_args: Any) -> None:
        logger.info("Received termination signal. Shutting down worker gracefully...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_exit_signal)
        except (NotImplementedError, RuntimeError):
            # Fallback for platforms/environments without add_signal_handler support
            signal.signal(sig, _handle_exit_signal)

    interval = max(5, settings.SWEEP_INTERVAL_SECONDS)
    sm = get_sessionmaker()
    timers = {"anchor": float("-inf"), "witness": float("-inf")}

    while not stop_event.is_set():
        await sweep_once(sm, timers, loop.time())

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    logger.info("ComputeCoin Background Worker exited cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
