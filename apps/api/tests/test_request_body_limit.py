"""A chunked upload (no Content-Length) must be refused at MAX_REQUEST_BYTES, like a declared one."""
import pytest

from app.core.config import settings


@pytest.mark.asyncio
async def test_chunked_body_over_limit_is_413(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_REQUEST_BYTES", 1024)

    async def gen():
        for _ in range(8):
            yield b"x" * 512

    r = await client.post("/auth/login", content=gen(), headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert r.json()["detail"] == "request_too_large"


@pytest.mark.asyncio
async def test_declared_body_over_limit_is_413(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_REQUEST_BYTES", 1024)
    r = await client.post("/auth/login", content=b"x" * 4096, headers={"content-type": "application/json"})
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_small_chunked_body_still_served(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_REQUEST_BYTES", 1024)

    async def gen():
        yield b'{"email":"a@b.co","password":"x"}'

    r = await client.post("/auth/login", content=gen(), headers={"content-type": "application/json"})
    assert r.status_code != 413
