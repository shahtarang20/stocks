"""Production health check endpoint verifying database, Redis cache, and services."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core.config import settings
from app.db.session import get_db

logger = logging.getLogger("computecoin.health")

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Production health check verifying database and Redis connectivity."""
    db_ok = False
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        logger.error("Health check DB probe failed: %s", exc)
        db_ok = False

    redis_ok = "not_configured"
    if settings.REDIS_URL and not settings.is_test:
        try:
            from redis.asyncio import from_url
            r = from_url(settings.REDIS_URL, decode_responses=True)
            pong = await r.ping()
            await r.aclose()
            redis_ok = "up" if pong else "down"
        except Exception as exc:
            logger.warning("Health check Redis probe failed: %s", exc)
            redis_ok = "down"

    is_healthy = db_ok and (redis_ok != "down" or not settings.is_production)
    if not is_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if is_healthy else "degraded",
        "db": "up" if db_ok else "down",
        "redis": redis_ok,
        "sentry": "enabled" if bool(settings.SENTRY_DSN) else "disabled",
        "email_provider": settings.EMAIL_PROVIDER,
        "environment": settings.ENVIRONMENT,
        "version": __version__,
    }
