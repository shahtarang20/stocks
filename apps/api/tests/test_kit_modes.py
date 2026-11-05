"""Kit modes batch / service / session (written, NOT run)."""

from __future__ import annotations

import pytest

from app.services.kits import validate_manifest
from tests.test_kits_u1 import DIGEST, _admin, _errs, _manifest, _publish, _spec_body, signer  # noqa: F401

SERVICE = {"port": 8000, "health_path": "/health", "max_concurrency": 4}
SESSION = {"mode": "tcp", "tcp_port": 8888, "command": ["jupyter", "lab"]}


def _codes(m):
    return {(e["field"], e["code"]) for e in validate_manifest(m)}


def test_batch_unchanged():
    assert validate_manifest(_manifest()) == []


def test_service_mode_needs_service_block():
    assert ("manifest.service", "service_block_required_for_mode_service") in _codes(_manifest(mode="service"))
    assert validate_manifest(_manifest(mode="service", service=SERVICE)) == []


@pytest.mark.parametrize("block", [
    {"port": 0, "health_path": "/h"}, {"port": True, "health_path": "/h"}, {"port": 80, "health_path": "h"},
    {"port": 80, "health_path": "/a/../b"}, {"port": 80, "health_path": "/h", "max_concurrency": 65},
    {"port": 80, "health_path": "/h", "extra": 1}, {"health_path": "/h"}, "x",
])
def test_bad_service_blocks_refused(block):
    assert validate_manifest(_manifest(mode="service", service=block))


def test_service_block_only_with_service_mode():
    assert ("manifest.service", "service_block_requires_mode_service") in _codes(_manifest(service=SERVICE))


def test_session_mode_needs_session_block_and_vice_versa():
    assert ("manifest.session", "session_block_required_for_mode_session") in _codes(_manifest(mode="session"))
    assert validate_manifest(_manifest(mode="session", session=SESSION)) == []
    assert ("manifest.session", "session_block_requires_mode_session") in _codes(_manifest(session=SESSION))


def test_unknown_mode_refused():
    assert ("manifest.mode", "mode_not_supported_yet") in _codes(_manifest(mode="vm"))


@pytest.mark.asyncio
async def test_service_kit_refused_by_jobs_spec_validation(client, signer):  # noqa: F811
    h = await _admin(client)
    m = _manifest(mode="service", service=SERVICE, inputs=[], outputs=[])
    assert (await _publish(client, h, signer, m)).status_code == 201
    body = _spec_body(inputs=[], outputs=[])
    r = await client.post("/v1/kits/validate-spec", headers=h, json=body)
    assert r.status_code == 422 and ("mode", "mode_not_batch") in _errs(r)
    r = await client.post("/v1/jobs/v2", headers=h, json={"spec": body})
    assert r.status_code == 422 and ("mode", "mode_not_batch") in _errs(r)


@pytest.mark.asyncio
async def test_session_kit_refused_by_jobs_and_batch_kit_refused_by_sessions(client, signer):  # noqa: F811
    h = await _admin(client)
    sm = _manifest(slug="nb", version="1.0.0", mode="session", session=SESSION, inputs=[], outputs=[])
    assert (await _publish(client, h, signer, sm)).status_code == 201
    assert (await _publish(client, h, signer, _manifest())).status_code == 201
    r = await client.post("/v1/jobs/v2", headers=h, json={"spec": _spec_body(kit="nb@1.0.0", inputs=[], outputs=[])})
    assert r.status_code == 422 and ("mode", "mode_not_batch") in _errs(r)
    r = await client.post("/v1/sessions", headers=h, json={"kit_ref": "blender-render@4.2.0", "gpu_count": 1,
                                                          "max_price_per_gpu_hour": "1", "client_pubkey": "a" * 64})
    assert r.status_code == 422 and "kit_mode_not_session" in r.text


@pytest.mark.asyncio
async def test_kit_list_exposes_mode_so_the_web_can_route_session_kits_to_sessions(client, signer):  # noqa: F811
    """The console catalogue starts a Kit as a job. A session Kit cannot be started that way (the form used to fail only at
    the very end), so the list must say which mode each Kit has."""
    h = await _admin(client)
    assert (await _publish(client, h, signer, _manifest(slug="batchy"))).status_code == 201
    m = _manifest(slug="termy", mode="session", session={"mode": "terminal"}, inputs=[], outputs=[])
    assert (await _publish(client, h, signer, m)).status_code == 201
    r = await client.get("/v1/kits")
    modes = {k["slug"]: k["mode"] for k in r.json()}
    assert modes == {"batchy": "batch", "termy": "session"}
    r = await client.get("/v1/kits/termy")
    assert r.json()[0]["mode"] == "session"
