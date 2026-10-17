"""FastAPI application entrypoint. Importable as ``app.main:app``."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
)
from app.core.config import settings

logging.basicConfig(level=settings.LOG_LEVEL.upper())


def create_app() -> FastAPI:
    application = FastAPI(
        title="ComputeCoin API",
        version=__version__,
        description="ComputeCoin GPU compute marketplace API (through Phase 6).",
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.FRONTEND_URL],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
    application.include_router(providers.billing_router)
    application.include_router(admin.money_router)

    @application.get("/")
    async def root() -> dict:
        return {"service": "computecoin-api", "version": __version__}

    return application


app = create_app()
