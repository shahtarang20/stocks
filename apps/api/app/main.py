"""FastAPI application entrypoint. Importable as ``app.main:app``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from sqlalchemy.exc import DBAPIError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routers import (
    admin,
    agent_releases,
    agent_v2,
    multi_node,
    agents,
    analytics,
    artifacts,
    auth,
    batch,
    billing,
    chain_recon,
    connect,
    data_plane,
    datasets,
    disputes,
    health,
    inference,
    invoices,
    jobs,
    fanout,
    pipelines,
    jobs_v2,
    unit_quotes,
    sessions,
    kits,
    marketplace,
    metrics,
    orgs,
    projects,
    provider_pricing,
    providers,
    quotes,
    reputation,
    reservations,
    workload_images,
    privacy,
    settlement,
    wallet,
    spend_limits,
    sso,
    telemetry,
    vendors,
    verification,
    webhooks,
    withdrawals,
    ws,
)
from app.core.config import settings
from app.core.logging import setup_logging

setup_logging()
logger = logging.getLogger("computecoin.sweeper")

if settings.SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.ENVIRONMENT,
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
            profiles_sample_rate=settings.SENTRY_PROFILES_SAMPLE_RATE,
            send_default_pii=False,
        )
        logging.getLogger("computecoin.api").info("Sentry monitoring initialized for %s", settings.ENVIRONMENT)
    except Exception as exc:
        logging.getLogger("computecoin.api").warning("Could not initialize Sentry: %s", exc)


async def _recovery_sweeper() -> None:
    """Background loop that recovers expired leases / stuck RESERVED jobs and
    re-drives stuck QUEUED jobs, so a disconnected provider never strands a job
    or leaks held credits/capacity (audit H5)."""
    from app.db.session import get_sessionmaker
    from app.services.leases import run_recovery_cycle
    from app.services.webhooks import deliver_due

    interval = settings.SWEEP_INTERVAL_SECONDS
    sm = get_sessionmaker()
    while True:
        try:
            async with sm() as db:
                await run_recovery_cycle(db)
                await db.commit()
                # Drain the webhook outbox on the same cadence. Isolated in its
                # own try/commit so an unreachable customer sink can never roll
                # back or stall lease recovery.
                try:
                    await deliver_due(db)
                    await db.commit()
                except Exception:  # pragma: no cover - defensive
                    await db.rollback()
                    logger.exception("webhook delivery sweep failed")
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive; never kill the loop
            logger.exception("recovery sweep failed")
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    task: asyncio.Task | None = None
    # Never run the sweeper under the test harness (shared in-memory DB).
    if not settings.is_test and settings.SWEEP_INTERVAL_SECONDS > 0:
        task = asyncio.create_task(_recovery_sweeper())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


class _StreamedBodyLimit:
    """Pure ASGI guard: refuse a request whose streamed body exceeds MAX_REQUEST_BYTES with 413.

    The Content-Length check in ``_limit_request_body`` is skipped by a chunked upload (no Content-Length header),
    so the bytes are counted as they arrive. On overflow the app sees a disconnect (it stops reading) and whatever
    it answers is replaced by a 413.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        total = 0
        too_large = False
        started = False  # a response (the app's, or our 413) has begun
        sent_413 = False

        async def counting_receive():
            nonlocal total, too_large
            if too_large:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > settings.MAX_REQUEST_BYTES:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        async def send_413() -> None:
            nonlocal started, sent_413
            sent_413 = True
            payload = b'{"detail":"request_too_large"}'
            started = True
            await send({"type": "http.response.start", "status": 413, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]})
            await send({"type": "http.response.body", "body": payload})

        async def tracking_send(message) -> None:
            nonlocal started
            if sent_413:
                return  # the app's own (error) response is replaced by the 413
            if too_large and not started:
                if message["type"] == "http.response.start":
                    await send_413()
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except Exception:
            if too_large and not started:
                await send_413()
                return
            raise


def create_app() -> FastAPI:
    # Expose interactive API docs only in dev-like environments.
    _is_dev = settings.ENVIRONMENT.lower() in {"development", "dev", "test", "testing", "local"}
    _docs = {} if _is_dev else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    application = FastAPI(
        title="ComputeCoin API",
        version=__version__,
        description="ComputeCoin GPU compute marketplace API (through Phase 10).",
        lifespan=lifespan,
        **_docs,
    )

    cors_origins = [orig.strip() for orig in settings.FRONTEND_URL.split(",") if orig.strip()]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=settings.CORS_ALLOW_ORIGIN_REGEX or None,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.exception_handler(DBAPIError)
    async def _reject_nul_characters(request: Request, exc: DBAPIError):
        """Postgres cannot store a NUL character in a text column. Any free-text
        field (project name, token label, org name, ...) given "\\u0000" used to fail
        the INSERT and return a bare 500; it is bad input, so answer 422 once, here,
        for every endpoint. Every other database error still surfaces as a 500."""
        orig = getattr(exc, "orig", None)
        if type(orig).__name__ == "CharacterNotInRepertoireError" or "0x00" in str(orig):
            return JSONResponse({"detail": "invalid_characters"}, status_code=422)
        raise exc

    @application.middleware("http")
    async def _limit_request_body(request: Request, call_next):
        """Reject over-large requests by declared Content-Length before the body
        is buffered into memory (DoS guard)."""
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > settings.MAX_REQUEST_BYTES:
                    return JSONResponse({"detail": "request_too_large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "invalid_content_length"}, status_code=400)
        return await call_next(request)

    # The Content-Length check above is skipped by a chunked upload (no Content-Length): count the bytes as they arrive.
    application.add_middleware(_StreamedBodyLimit)

    application.include_router(health.router)
    application.include_router(metrics.router)
    application.include_router(auth.router)
    application.include_router(projects.router)
    application.include_router(providers.router)
    application.include_router(agents.router)
    application.include_router(admin.router)
    application.include_router(marketplace.router)
    application.include_router(quotes.router)
    application.include_router(reservations.router)
    application.include_router(jobs.router)
    application.include_router(artifacts.router)
    application.include_router(billing.router)
    application.include_router(invoices.router)
    application.include_router(disputes.router)
    application.include_router(inference.router)
    application.include_router(workload_images.router)
    application.include_router(telemetry.router)
    from app.api.routers import observability as _observability

    application.include_router(_observability.router)
    from app.api.routers import abuse as _abuse

    application.include_router(_abuse.router)
    application.include_router(settlement.router)
    application.include_router(wallet.router)
    application.include_router(wallet.admin_router)
    application.include_router(providers.billing_router)
    application.include_router(admin.money_router)

    # --- Phases B/C/D -----------------------------------------------------
    # Each router declares its own prefix; none collides with the routes above
    # (disputes.admin_router uses /v1/admin/disputes/{id}/adjudicate, which is
    # deliberately distinct from admin.money_router's legacy /resolve path).
    application.include_router(verification.router)
    application.include_router(reputation.router)
    application.include_router(spend_limits.router)
    application.include_router(analytics.router)
    application.include_router(webhooks.router)
    application.include_router(disputes.admin_router)
    application.include_router(ws.router)
    application.include_router(batch.router)
    application.include_router(datasets.router)
    application.include_router(datasets.training_router)
    application.include_router(orgs.router)
    application.include_router(sso.router)
    application.include_router(provider_pricing.router)
    application.include_router(connect.router)
    application.include_router(chain_recon.router)
    application.include_router(vendors.router)
    application.include_router(privacy.router)
    application.include_router(withdrawals.router)
    application.include_router(withdrawals.admin_router)
    application.include_router(data_plane.router)
    application.include_router(kits.router)
    application.include_router(kits.admin_router)
    application.include_router(agent_v2.router)
    application.include_router(multi_node.router)
    application.include_router(multi_node.admin_router)
    application.include_router(agent_releases.admin_router)
    application.include_router(agent_releases.host_router)
    application.include_router(jobs_v2.router)
    from app.api.routers import byoc as _byoc  # U8

    application.include_router(_byoc.router)
    application.include_router(_byoc.admin_router)
    application.include_router(fanout.router)
    from app.api.routers import services as _services

    application.include_router(_services.router)
    application.include_router(_services.agent_router)
    application.include_router(pipelines.router)
    application.include_router(unit_quotes.router)
    application.include_router(sessions.router)

    @application.get("/")
    async def root() -> dict:
        return {"service": "computecoin-api", "version": __version__}

    return application


app = create_app()
