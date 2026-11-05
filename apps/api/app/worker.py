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
from app.services.stake_sync import run_stake_sync_cycle
from app.services.token_deposits import scan_deposits
from app.services.chain_reconciler import reconcile_withdrawals
from app.services.data_plane import sweep_data_plane
from app.services.fanout import sweep_fanouts
from app.services.pipelines import sweep_pipelines
from app.services.service_runtime import sweep_services
from app.services.withdrawals import run_payout_cycle

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

    if settings.withdrawals_configured:
        # B1: its own try/except. A payout or chain failure never stops other steps; it is retried next sweep.
        try:
            async with sm() as db:
                payout = await run_payout_cycle(db)
                await db.commit()
                if any(k in payout for k in ("sent", "paid_out", "refunded", "stuck", "error", "blocked")):
                    logger.info("Withdrawal payout cycle: %s", payout)
        except Exception as exc:
            logger.exception("Withdrawal payout cycle failed (will retry next sweep): %s", exc)
        if now - timers.get("withdrawal_recon", float("-inf")) >= max(5, settings.CHAIN_RECONCILE_INTERVAL_SECONDS):
            timers["withdrawal_recon"] = now
            try:
                async with sm() as db:
                    recon = await reconcile_withdrawals(db)  # a mismatch pauses withdrawals itself
                    await db.commit()
                    if recon.get("enabled") and not recon.get("ok"):
                        logger.error("Withdrawal reconciliation MISMATCH, withdrawals paused: %s", recon)
            except Exception as exc:
                logger.exception("Withdrawal reconciliation failed: %s", exc)

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

    if settings.STAKE_SYNC_ENABLED:
        if now - timers.get("stake", float("-inf")) >= max(5, settings.STAKE_SYNC_INTERVAL_SECONDS):
            # Own try/except and timer: a chain outage must not stop lease recovery or other steps.
            timers["stake"] = now
            try:
                async with sm() as db:
                    stake_res = await run_stake_sync_cycle(db)
                    await db.commit()
                    if stake_res.get("changed") or stake_res.get("errors"):
                        logger.info("Stake sync: %s", stake_res)
            except Exception as exc:
                logger.exception("Stake sync cycle failed: %s", exc)

    if settings.data_plane_configured:
        # U2: its own try/except and timer. A storage outage never stops lease recovery or other steps.
        if now - timers.get("data_plane", float("-inf")) >= max(5, settings.DATA_PLANE_SWEEP_INTERVAL_SECONDS):
            timers["data_plane"] = now
            try:
                async with sm() as db:
                    dp = await sweep_data_plane(db)
                    await db.commit()
                    if any(dp.values()):
                        logger.info("Data plane sweep: %s", dp)
            except Exception as exc:
                logger.exception("Data plane sweep failed (will retry next interval): %s", exc)

    # U16: abuse detector sweep. Observe-only unless ABUSE_ENFORCE=true. Own try/except and timer.
    if now - timers.get("abuse", float("-inf")) >= 60:
        timers["abuse"] = now
        try:
            from app.services.abuse import sweep as abuse_sweep

            async with sm() as db:
                flagged = await abuse_sweep(db)
                await db.commit()
                if flagged:
                    logger.warning("Abuse detector raised %d new flag(s)", flagged)
        except Exception as exc:
            logger.exception("Abuse sweep failed (will retry next interval): %s", exc)

    # U10: stop sessions past a deadline (idle, max duration, host lost, never claimed) even when nobody is polling.
    if now - timers.get("sessions", float("-inf")) >= max(5, settings.SESSIONS_REAP_INTERVAL_SECONDS):
        timers["sessions"] = now
        try:
            from app.services import sessions as sessions_svc
            from app.services.session_relay import get_relay

            async with sm() as db:
                reaped = await sessions_svc.reap(db, get_relay())
                await db.commit()
                if reaped:
                    logger.info("Session reaper stopped %d session(s)", reaped)
        except Exception as exc:
            logger.exception("Session reaper failed (will retry next interval): %s", exc)

    if settings.data_plane_configured:
        # U4: fan-out parts, straggler copies and merge. Its own try/except and timer: a failing fan-out never
        # stops lease recovery or any other step, and each group is also isolated inside the sweep.
        if now - timers.get("fanout", float("-inf")) >= max(5, settings.FANOUT_SWEEP_INTERVAL_SECONDS):
            timers["fanout"] = now
            try:
                fo = await sweep_fanouts(sm)
                if any(v for k, v in fo.items() if k != "groups"):
                    logger.info("Fan-out sweep: %s", fo)
            except Exception as exc:
                logger.exception("Fan-out sweep failed (will retry next interval): %s", exc)

        # U9: pipeline steps (start ready steps, collect outputs, skip after a failure). Its own try/except and timer.
        if now - timers.get("pipelines", float("-inf")) >= max(5, settings.PIPELINE_SWEEP_INTERVAL_SECONDS):
            timers["pipelines"] = now
            try:
                pl = await sweep_pipelines(sm)
                if any(v for k, v in pl.items() if k != "runs"):
                    logger.info("Pipeline sweep: %s", pl)
            except Exception as exc:
                logger.exception("Pipeline sweep failed (will retry next interval): %s", exc)

    # U11: services autoscaling and failover. Its own try/except and timer; off unless SERVICES_ENABLED.
    if settings.SERVICES_ENABLED and now - timers.get("services", float("-inf")) >= max(2, settings.SERVICE_SWEEP_INTERVAL_SECONDS):
        timers["services"] = now
        try:
            sv = await sweep_services(sm)
            if any(v for k, v in sv.items() if k != "services"):
                logger.info("Services sweep: %s", sv)
        except Exception as exc:
            logger.exception("Services sweep failed (will retry next interval): %s", exc)

    if settings.data_plane_configured and settings.KIT_VERIFICATION_SAMPLE_RATE > 0:
        # U6: judge Kit reference re-runs. Its own try/except and timer; each check is also isolated inside.
        if now - timers.get("kit_verification", float("-inf")) >= max(5, settings.KIT_VERIFICATION_SWEEP_INTERVAL_SECONDS):
            timers["kit_verification"] = now
            try:
                from app.services.kit_verification_run import sweep_kit_verifications

                kv = await sweep_kit_verifications(sm)
                if kv.get("resolved") or kv.get("errors"):
                    logger.info("Kit verification sweep: %s", kv)
            except Exception as exc:
                logger.exception("Kit verification sweep failed (will retry next interval): %s", exc)

    # U13: a failed or cancelled rank cancels its sibling ranks and releases their holds. Its own try/except and timer.
    if settings.MULTI_NODE_ENABLED and now - timers.get("multi_node", float("-inf")) >= 5:
        timers["multi_node"] = now
        try:
            from app.services.multi_node import sweep_rank_failures

            mn = await sweep_rank_failures(sm)
            if mn.get("cancelled") or mn.get("errors"):
                logger.info("Multi-node rank failure sweep: %s", mn)
        except Exception as exc:
            logger.exception("Multi-node rank failure sweep failed (will retry next interval): %s", exc)

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
    timers = {"anchor": float("-inf"), "witness": float("-inf"), "stake": float("-inf")}

    while not stop_event.is_set():
        await sweep_once(sm, timers, loop.time())

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    logger.info("ComputeCoin Background Worker exited cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
