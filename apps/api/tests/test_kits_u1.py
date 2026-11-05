"""U1: Kit registry and Job Spec v2 (ADR-0006 checkpoints U1.1-U1.4). Fresh random keys only (L7)."""

from __future__ import annotations

import copy
import importlib.util
import pathlib

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from app.core.config import settings
from app.db.session import get_sessionmaker
from app.models.enums import Role
from app.models.kits import Kit, KitImmutableError
from app.services.kits import SIGN_PREFIX, canonical_manifest_bytes
from tests.helpers import auth_headers, grant_role, register_verify_login

_spec = importlib.util.spec_from_file_location(
    "sign_kit", pathlib.Path(__file__).resolve().parents[3] / "scripts" / "sign_kit.py"
)
sign_kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sign_kit)

DIGEST = "registry.example/blender@sha256:" + "a" * 64


def _manifest(version="4.2.0", **over):
    m = {
        "schema": 1, "slug": "blender-render", "version": version, "name": "Blender render",
        "description": "d", "category": "3d", "image": DIGEST, "mode": "batch",
        "param_schema": {
            "type": "object", "additionalProperties": False, "required": ["frames"],
            "properties": {"frames": {"type": "integer", "minimum": 1, "maximum": 1000},
                           "engine": {"type": "string", "enum": ["cycles", "eevee"]}},
        },
        "inputs": [{"name": "scene", "kind": "dataset", "required": True}],
        "outputs": [{"name": "frames", "path": "/out/frames/*.png"}],
        "resource_defaults": {"gpu_count": 1, "min_vram_gb": 8, "cpu": 4, "ram_gb": 16, "disk_gb": 50},
        "billing_hints": {"unit": "frame", "est_gpu_seconds_per_unit": "30"},
        "verification": {"method": "perceptual"},
    }
    m.update(over)
    return m


@pytest.fixture
def signer(monkeypatch):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(settings, "KIT_SIGNING_PUBLIC_KEY", sign_kit._pub_hex(key))
    return key


def _sign(key, manifest):
    return key.sign(SIGN_PREFIX + canonical_manifest_bytes(manifest)).hex()


async def _admin(client):
    await register_verify_login(client, "u1-admin@example.com")
    await grant_role("u1-admin@example.com", Role.administrator)
    from tests.helpers import login
    return auth_headers((await login(client, "u1-admin@example.com"))["access_token"])


async def _publish(client, h, key, manifest=None):
    manifest = manifest or _manifest()
    return await client.post("/v1/admin/kits", headers=h, json={"manifest": manifest, "signature": _sign(key, manifest)})


def _errs(resp):
    return {(e["field"], e["code"]) for e in resp.json()["detail"]["errors"]}


def _spec_body(**over):
    s = {"version": 2, "kit": "blender-render@4.2.0", "inputs": [{"name": "scene", "source": "dataset:ds_1"}],
         "outputs": [{"name": "frames"}], "params": {"frames": 10}}
    s.update(over)
    return s


@pytest.mark.asyncio
async def test_publish_and_public_read(client, signer):
    h = await _admin(client)
    r = await _publish(client, h, signer)
    assert r.status_code == 201, r.text
    assert (await client.get("/v1/kits")).json()[0]["slug"] == "blender-render"
    d = await client.get("/v1/kits/blender-render/4.2.0")
    assert d.status_code == 200 and d.json()["image_digest"] == DIGEST
    assert (await client.get("/v1/kits/nope/1")).status_code == 404


@pytest.mark.asyncio
async def test_unsigned_tampered_and_wrong_key_refused(client, signer):
    h = await _admin(client)
    m = _manifest()
    good = _sign(signer, m)
    tampered = _manifest(name="Evil")
    assert (await client.post("/v1/admin/kits", headers=h, json={"manifest": tampered, "signature": good})).status_code == 422
    other = Ed25519PrivateKey.generate()
    assert (await client.post("/v1/admin/kits", headers=h, json={"manifest": m, "signature": _sign(other, m)})).status_code == 422
    assert (await client.post("/v1/admin/kits", headers=h, json={"manifest": m, "signature": "zz"})).status_code == 422


@pytest.mark.asyncio
async def test_no_configured_key_fails_closed(client, monkeypatch):
    monkeypatch.setattr(settings, "KIT_SIGNING_PUBLIC_KEY", "")
    h = await _admin(client)
    k = Ed25519PrivateKey.generate()
    assert (await _publish(client, h, k)).status_code == 503


@pytest.mark.asyncio
async def test_non_admin_cannot_publish(client, signer):
    t = (await register_verify_login(client, "u1-cust@example.com"))["access_token"]
    assert (await _publish(client, auth_headers(t), signer)).status_code == 403


@pytest.mark.asyncio
async def test_bad_manifests_refused(client, signer):
    h = await _admin(client)
    r = await _publish(client, h, signer, _manifest(image="registry.example/blender:latest"))
    assert ("manifest.image", "image_must_be_pinned_by_sha256_digest") in _errs(r)
    ps = copy.deepcopy(_manifest()["param_schema"])
    ps["properties"]["x"] = {"$ref": "https://evil.example/s.json"}
    r = await _publish(client, h, signer, _manifest(param_schema=ps))
    assert any(c == "schema_remote_ref_refused" for _, c in _errs(r))
    r = await _publish(client, h, signer, _manifest(param_schema={"type": "object"}))
    assert r.status_code == 422
    r = await _publish(client, h, signer, _manifest(outputs=[{"name": "f", "path": "/etc/passwd"}]))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_version_immutable(client, signer):
    h = await _admin(client)
    assert (await _publish(client, h, signer)).status_code == 201
    assert (await _publish(client, h, signer, _manifest(description="changed"))).status_code == 409
    assert (await _publish(client, h, signer, _manifest(version="4.2.1"))).status_code == 201
    sm = get_sessionmaker()
    async with sm() as s:
        k = (await s.execute(select(Kit).where(Kit.version == "4.2.0"))).scalar_one()
        k.image_digest = "x@sha256:" + "b" * 64
        with pytest.raises(KitImmutableError):
            await s.flush()
        await s.rollback()
        k = (await s.execute(select(Kit).where(Kit.version == "4.2.0"))).scalar_one()
        k.status = "yanked"  # the one allowed change
        await s.flush()
        await s.rollback()


@pytest.mark.asyncio
async def test_valid_spec_and_field_errors(client, signer):
    h = await _admin(client)
    await _publish(client, h, signer)
    ok = await client.post("/v1/kits/validate-spec", headers=h, json=_spec_body(resources={"gpu": {"min_vram_gb": 24}}))
    assert ok.status_code == 200, ok.text
    assert ok.json()["effective_resources"]["min_vram_gb"] == 24

    cases = [
        (_spec_body(params={"frames": 0}), ("params.frames", "schema_minimum")),
        (_spec_body(params={"frames": 5, "bogus": 1}), ("params", "unknown_field")),
        (_spec_body(params={}), ("params", "schema_required")),
        (_spec_body(bogus=1), ("bogus", "unknown_field")),
        (_spec_body(kit="nope@1"), ("kit", "kit_not_found_or_unavailable")),
        (_spec_body(kit="BAD"), ("kit", "value_error")),
        (_spec_body(resources={"gpu": {"min_vram_gb": 4}}), ("resources.gpu.min_vram_gb", "below_kit_minimum")),
        (_spec_body(inputs=[]), ("inputs", "missing_required_input")),
        (_spec_body(inputs=[{"name": "scene", "source": "dataset:a"}, {"name": "x", "source": "dataset:b"}]),
         ("inputs[1].name", "unknown_input")),
        (_spec_body(outputs=[{"name": "nope"}]), ("outputs[0].name", "unknown_output")),
        (_spec_body(billing={"unit": "image"}), ("billing.unit", "unit_not_offered_by_kit")),
        (_spec_body(inputs=[{"name": "scene", "source": "http://evil/x"}]), ("inputs.0.source", "value_error")),
        ({**_spec_body(), "image": "x@sha256:" + "a" * 64}, ("image", "not_supported_yet")),
        (_spec_body(mode="service"), ("mode", "literal_error")),
    ]
    for body, expected in cases:
        r = await client.post("/v1/kits/validate-spec", headers=h, json=body)
        assert r.status_code == 422, (body, r.text)
        assert expected in _errs(r), (expected, r.json())


@pytest.mark.asyncio
async def test_yanked_and_db_tampered_kit_refused_at_use(client, signer):
    h = await _admin(client)
    await _publish(client, h, signer)
    await _publish(client, h, signer, _manifest(version="4.2.1"))
    assert (await client.post("/v1/admin/kits/blender-render/4.2.1/yank", headers=h)).status_code == 200
    r = await client.post("/v1/kits/validate-spec", headers=h, json=_spec_body(kit="blender-render@4.2.1"))
    assert ("kit", "kit_not_found_or_unavailable") in _errs(r)

    sm = get_sessionmaker()
    async with sm() as s:  # bypass the ORM guard with a Core update, as an attacker with DB access might
        from sqlalchemy import update
        bad = _manifest()
        bad["param_schema"] = {"type": "object", "additionalProperties": True}
        await s.execute(update(Kit).where(Kit.version == "4.2.0").values(manifest=bad))
        await s.commit()
    r = await client.post("/v1/kits/validate-spec", headers=h, json=_spec_body())
    assert r.status_code == 422 and ("kit", "kit_integrity_failed") in _errs(r)


def test_sign_script_matches_server_scheme(signer):
    m = _manifest()
    assert sign_kit.SIGN_PREFIX == SIGN_PREFIX
    assert sign_kit.canonical(m) == canonical_manifest_bytes(m)
