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

setup_logging()
logger = logging.getLogger("computecoin.worker")


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

    while not stop_event.is_set():
        try:
            async with sm() as db:
                recovered = await run_recovery_cycle(db)
                await db.commit()
                if any(recovered.values()):
                    logger.info("Recovery sweep completed: %s", recovered)
        except Exception as exc:
            logger.exception("Worker recovery cycle encountered an error: %s", exc)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    logger.info("ComputeCoin Background Worker exited cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
