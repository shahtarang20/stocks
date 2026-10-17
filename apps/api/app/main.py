"""FastAPI application entrypoint. Importable as ``app.main:app``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routers import (
    admin,
    agents,
    artifacts,
    auth,
    billing,
    disputes,
    health,
    inference,
    invoices,
    jobs,
    marketplace,
    projects,
    providers,
    quotes,
    reservations,
    settlement,
    telemetry,
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

    interval = settings.SWEEP_INTERVAL_SECONDS
    sm = get_sessionmaker()
    while True:
        try:
            async with sm() as db:
                await run_recovery_cycle(db)
                await db.commit()
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
        allow_origin_regex=r"^https:\/\/.*\.vercel\.app$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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

    application.include_router(health.router)
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
    application.include_router(telemetry.router)
    application.include_router(settlement.router)
    application.include_router(providers.billing_router)
    application.include_router(admin.money_router)

    @application.get("/")
    async def root() -> dict:
        return {"service": "computecoin-api", "version": __version__}

    return application


app = create_app()
