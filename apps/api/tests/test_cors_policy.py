"""CORS: only the configured origins get credentialed access; a blanket *.vercel.app pattern must not be allowed."""

import pytest

from app.core.config import settings


@pytest.mark.asyncio
async def test_unlisted_vercel_origin_gets_no_cors_headers(client):
    r = await client.options(
        "/health",
        headers={"Origin": "https://evil-page.vercel.app", "Access-Control-Request-Method": "GET"},
    )
    # Without Access-Control-Allow-Origin the browser blocks the request (Starlette answers the preflight with 400).
    assert "access-control-allow-origin" not in r.headers
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_configured_frontend_origin_is_allowed_with_credentials(client):
    origin = settings.FRONTEND_URL.split(",")[0].strip()
    r = await client.options("/health", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") == origin
    assert r.headers.get("access-control-allow-credentials") == "true"
