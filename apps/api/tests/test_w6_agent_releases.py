"""W6/W10: agent release management. Signed manifests per platform and ring, pause, rollback, ring selection.

Fresh random Ed25519 keys only (L7). WRITTEN, NOT RUN (the task forbids running tests).
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.config import settings
from app.db.base import utcnow
from app.models.enums import Role
from app.services import agent_releases as svc
from tests.helpers import auth_headers, grant_role, login, register_verify_login
from tests.test_phase3_marketplace import create_listable_machine

URL = "https://downloads.example.test/ComputeCoinAgent-{v}.msi"


def _pub_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


@pytest.fixture
def signer(monkeypatch):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(settings, "AGENT_RELEASE_SIGNING_PUBLIC_KEY", _pub_hex(key))
    return key


def _manifest(version="1.1.0", ring="general", platform="windows", floor="1.0.0", **over):
    m = {
        "schema": 1, "product": "computecoin-agent", "version": version, "platform": platform,
        "artifact_url": URL.format(v=version), "sha256": "ab" * 32, "size_bytes": 1234567,
        "min_supported": floor, "ring": ring, "issued_at": 1_800_000_000,
    }
    m.update(over)
    return m


def _sign(key, manifest) -> str:
    return key.sign(svc.SIGN_PREFIX + svc.canonical_bytes(manifest)).hex()


async def _admin(client, email="rel-admin@example.com"):
    await register_verify_login(client, email)
    await grant_role(email, Role.administrator)
    return auth_headers((await login(client, email))["access_token"])


async def _publish(client, h, key, **kw):
    m = _manifest(**kw)
    return await client.post("/v1/admin/agent-releases", headers=h, json={"manifest": m, "signature": _sign(key, m)})


def _machine_headers(machine_id: str, secret: str) -> dict:
    """Machine HMAC for a GET: the body is empty, so the signed body hash is sha256(b'')."""
    ts = str(int(utcnow().timestamp()))
    canonical = f"{machine_id}\n{ts}\n{hashlib.sha256(b'').hexdigest()}"
    sig = hmac.new(secret.removeprefix("msk_").encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {"X-ComputeCoin-Machine": machine_id, "X-ComputeCoin-Timestamp": ts, "X-ComputeCoin-Signature": sig}


async def _manifest_for(client, m, *, platform="windows", current="1.0.0", ring=None):
    params = {"platform": platform, "current_version": current}
    if ring:
        params["ring"] = ring
    return await client.get("/v1/agents/releases/manifest", params=params, headers=_machine_headers(m["machine_id"], m["secret"]))


# ---- signing ------------------------------------------------------------------------------------------------------------

async def test_publish_requires_valid_signature(client, signer):
    h = await _admin(client)
    m = _manifest()
    bad = await client.post("/v1/admin/agent-releases", headers=h, json={"manifest": m, "signature": "00" * 64})
    assert bad.status_code == 422 and bad.json()["detail"] == "agent_release_signature_invalid"
    other = Ed25519PrivateKey.generate()
    wrong = await client.post("/v1/admin/agent-releases", headers=h, json={"manifest": m, "signature": _sign(other, m)})
    assert wrong.status_code == 422 and wrong.json()["detail"] == "agent_release_signature_invalid"
    ok = await _publish(client, h, signer)
    assert ok.status_code == 201, ok.text


async def test_tampered_manifest_after_signing_is_refused(client, signer):
    h = await _admin(client)
    m = _manifest()
    sig = _sign(signer, m)
    m["artifact_url"] = "https://evil.example.test/agent.msi"  # changed after signing
    r = await client.post("/v1/admin/agent-releases", headers=h, json={"manifest": m, "signature": sig})
    assert r.status_code == 422 and r.json()["detail"] == "agent_release_signature_invalid"


async def test_no_signing_key_configured_fails_closed(client, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_RELEASE_SIGNING_PUBLIC_KEY", "")
    h = await _admin(client)
    key = Ed25519PrivateKey.generate()
    r = await _publish(client, h, key)
    assert r.status_code == 503 and r.json()["detail"] == "agent_release_signing_key_not_configured"


async def test_signed_but_structurally_bad_manifest_refused(client, signer):
    h = await _admin(client)
    for over, code in (
        ({"artifact_url": "http://plain.example.test/a.msi"}, "manifest_artifact_url"),
        ({"sha256": "XYZ"}, "manifest_sha256"),
        ({"ring": "beta"}, "manifest_platform_or_ring"),
        ({"floor": "9.0.0"}, "manifest_floor_above_version"),
    ):
        r = await _publish(client, h, signer, **over)
        assert r.status_code == 422 and r.json()["detail"] == code, (over, r.text)


async def test_admin_only_and_duplicate_conflict(client, signer):
    await register_verify_login(client, "rel-user@example.com")
    tok = (await login(client, "rel-user@example.com"))["access_token"]
    m = _manifest()
    denied = await client.post("/v1/admin/agent-releases", headers=auth_headers(tok), json={"manifest": m, "signature": _sign(signer, m)})
    assert denied.status_code == 403
    h = await _admin(client)
    assert (await _publish(client, h, signer)).status_code == 201
    assert (await _publish(client, h, signer)).status_code == 409


# ---- ring selection ------------------------------------------------------------------------------------------------------

async def test_ring_selection_default_general_sees_only_general(client, signer):
    h = await _admin(client)
    await _publish(client, h, signer, version="1.2.0", ring="canary")
    await _publish(client, h, signer, version="1.1.0", ring="general")
    m = await create_listable_machine(client, "ring-p1@example.com", hostname="r1")
    r = await _manifest_for(client, m)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == "1.1.0" and r.json()["ring"] == "general"


async def test_server_assigned_canary_gets_the_canary_build(client, signer):
    h = await _admin(client)
    await _publish(client, h, signer, version="1.2.0", ring="canary")
    await _publish(client, h, signer, version="1.1.0", ring="general")
    m = await create_listable_machine(client, "ring-p2@example.com", hostname="r2")
    put = await client.put(f"/v1/admin/agent-releases/rings/{m['machine_id']}", headers=h, json={"ring": "canary"})
    assert put.status_code == 200 and put.json()["ring"] == "canary"
    r = await _manifest_for(client, m)
    assert r.json()["version"] == "1.2.0"


async def test_host_report_can_only_widen_never_narrow_its_ring(client, signer):
    h = await _admin(client)
    await _publish(client, h, signer, version="1.2.0", ring="canary")
    await _publish(client, h, signer, version="1.1.0", ring="general")
    canary = await create_listable_machine(client, "ring-p3@example.com", hostname="r3")
    await client.put(f"/v1/admin/agent-releases/rings/{canary['machine_id']}", headers=h, json={"ring": "canary"})
    widened = await _manifest_for(client, canary, ring="general")  # a canary host that asks to be general: fewer releases
    assert widened.json()["version"] == "1.1.0"
    general = await create_listable_machine(client, "ring-p4@example.com", hostname="r4")
    narrowed = await _manifest_for(client, general, ring="canary")  # a general host claiming canary: ignored
    assert narrowed.json()["version"] == "1.1.0"


async def test_platform_isolation_and_nothing_newer_is_204(client, signer):
    h = await _admin(client)
    await _publish(client, h, signer, version="1.1.0", platform="linux")
    m = await create_listable_machine(client, "ring-p5@example.com", hostname="r5")
    assert (await _manifest_for(client, m, platform="windows")).status_code == 204
    await _publish(client, h, signer, version="1.1.0")
    assert (await _manifest_for(client, m, current="1.1.0")).status_code == 204  # already on it


async def test_manifest_route_needs_machine_auth(client, signer):
    r = await client.get("/v1/agents/releases/manifest", params={"platform": "windows", "current_version": "1.0.0"})
    assert r.status_code == 401


async def test_served_manifest_is_exactly_the_signed_bytes(client, signer):
    h = await _admin(client)
    m = _manifest()
    sig = _sign(signer, m)
    await client.post("/v1/admin/agent-releases", headers=h, json={"manifest": m, "signature": sig})
    mach = await create_listable_machine(client, "ring-p6@example.com", hostname="r6")
    body = (await _manifest_for(client, mach)).json()
    assert body.pop("signature") == sig and body == m  # the agent verifies these very bytes


# ---- pause / resume / rollback --------------------------------------------------------------------------------------------

async def test_pause_withholds_and_resume_restores(client, signer):
    h = await _admin(client)
    rel = (await _publish(client, h, signer, version="1.1.0")).json()
    mach = await create_listable_machine(client, "ring-p7@example.com", hostname="r7")
    assert (await client.post(f"/v1/admin/agent-releases/{rel['id']}/pause", headers=h, json={"reason": "bad canary"})).json()["status"] == "paused"
    assert (await _manifest_for(client, mach)).status_code == 204
    assert (await client.post(f"/v1/admin/agent-releases/{rel['id']}/resume", headers=h, json={})).json()["status"] == "published"
    assert (await _manifest_for(client, mach)).status_code == 200


async def test_rollback_is_final_and_older_release_is_not_a_downgrade_offer(client, signer):
    h = await _admin(client)
    await _publish(client, h, signer, version="1.0.5")
    bad = (await _publish(client, h, signer, version="1.1.0")).json()
    mach = await create_listable_machine(client, "ring-p8@example.com", hostname="r8")
    rb = await client.post(f"/v1/admin/agent-releases/{bad['id']}/rollback", headers=h, json={"reason": "crash loop"})
    assert rb.status_code == 200 and rb.json()["rolled_back"]["status"] == "rolled_back"
    # a host already on the bad 1.1.0 is NOT offered 1.0.5: an agent refuses a downgrade without a signed revocation
    assert (await _manifest_for(client, mach, current="1.1.0")).status_code == 204
    # and a rolled-back release can never be resumed
    again = await client.post(f"/v1/admin/agent-releases/{bad['id']}/resume", headers=h, json={})
    assert again.status_code == 409


async def test_rollback_with_signed_replacement_moves_hosts_off_the_bad_version(client, signer):
    h = await _admin(client)
    bad = (await _publish(client, h, signer, version="1.1.0")).json()
    fix = _manifest(version="1.1.1", revoked_versions=["1.1.0"])
    ok = await client.post(f"/v1/admin/agent-releases/{bad['id']}/rollback", headers=h,
                           json={"reason": "crash", "replacement": {"manifest": fix, "signature": _sign(signer, fix)}})
    assert ok.status_code == 200 and ok.json()["replacement"]["version"] == "1.1.1"
    mach = await create_listable_machine(client, "ring-p9@example.com", hostname="r9")
    assert (await _manifest_for(client, mach, current="1.1.0")).json()["version"] == "1.1.1"


async def test_rollback_replacement_must_revoke_and_be_newer(client, signer):
    h = await _admin(client)
    bad = (await _publish(client, h, signer, version="1.1.0")).json()
    no_revoke = _manifest(version="1.1.1")
    r1 = await client.post(f"/v1/admin/agent-releases/{bad['id']}/rollback", headers=h,
                           json={"replacement": {"manifest": no_revoke, "signature": _sign(signer, no_revoke)}})
    assert r1.status_code == 422 and r1.json()["detail"] == "replacement_must_revoke_rolled_back_version"
    older = _manifest(version="1.0.9", revoked_versions=["1.1.0"])
    r2 = await client.post(f"/v1/admin/agent-releases/{bad['id']}/rollback", headers=h,
                           json={"replacement": {"manifest": older, "signature": _sign(signer, older)}})
    assert r2.status_code == 422 and r2.json()["detail"] == "replacement_must_be_newer"
    listed = (await client.get("/v1/admin/agent-releases", headers=h)).json()
    assert [r["status"] for r in listed if r["version"] == "1.1.0"] == ["published"]  # a refused rollback changed nothing


async def test_manifest_content_is_immutable_after_publish(client, signer):
    from app.db.session import get_sessionmaker
    from app.models.agent_releases import AgentRelease, AgentReleaseImmutableError

    h = await _admin(client)
    rel = (await _publish(client, h, signer)).json()
    sm = get_sessionmaker()
    async with sm() as s:
        row = await s.get(AgentRelease, __import__("uuid").UUID(rel["id"]))
        row.artifact_sha256 = "cd" * 32
        with pytest.raises(AgentReleaseImmutableError):
            await s.flush()


# ---- pure functions ---------------------------------------------------------------------------------------------------------

def test_version_ordering_and_effective_ring():
    assert svc.version_key("1.2.0-rc1") < svc.version_key("1.2.0") < svc.version_key("1.10.0")
    assert svc.effective_ring("canary", "general") == "general"
    assert svc.effective_ring("general", "canary") == "general"
    assert svc.effective_ring("early", None) == "early"
    assert svc.effective_ring("early", "nonsense") == "early"
