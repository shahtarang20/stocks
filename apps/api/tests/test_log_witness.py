"""Tests for API log witness aggregation and threshold enforcement (WP-E / E4)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
import httpx
import pytest

from app.core.config import settings
from app.services.log_witness import (
    gather_witness_cosignatures,
    parse_configured_witnesses,
    WitnessThresholdNotMetError,
)
from app.services.privacy_crypto import canonical_json, generate_ed25519_keypair, sign


from app.db.session import get_sessionmaker


def test_parse_configured_witnesses():
    with patch.object(settings, "PRIVACY_WITNESSES", "{}"):
        assert parse_configured_witnesses() == {}

    with patch.object(settings, "PRIVACY_WITNESSES", "not-json"):
        assert parse_configured_witnesses() == {}

    valid_json = json.dumps({"w1": {"url": "http://witness1", "public_key": "aa" * 32}})
    with patch.object(settings, "PRIVACY_WITNESSES", valid_json):
        assert "w1" in parse_configured_witnesses()


@pytest.mark.asyncio
async def test_gather_witness_cosignatures_success(db_engine):
    sm = get_sessionmaker()
    async with sm() as db_session:
        w1_priv, w1_pub = generate_ed25519_keypair()
    w2_priv, w2_pub = generate_ed25519_keypair()
    log_pub = "11" * 32
    root = "22" * 32
    checkpoint = {"tree_size": 1, "root_hash": root, "log_public_key": log_pub}

    stmt1 = canonical_json({
        "v": "ccpp-witness-v1",
        "tree_size": 1,
        "root_hash": root.lower(),
        "log_public_key": log_pub.lower(),
        "witness": "w1",
    })
    stmt2 = canonical_json({
        "v": "ccpp-witness-v1",
        "tree_size": 1,
        "root_hash": root.lower(),
        "log_public_key": log_pub.lower(),
        "witness": "w2",
    })

    sig1 = sign(w1_priv, stmt1)
    sig2 = sign(w2_priv, stmt2)

    config_witnesses = {
        "w1": {"url": "http://w1.test", "public_key": w1_pub},
        "w2": {"url": "http://w2.test", "public_key": w2_pub},
    }

    async def mock_handler(request: httpx.Request):
        url = str(request.url)
        if "w1.test/state" in url:
            return httpx.Response(200, json={"tree_size": 0, "root_hash": ""})
        if "w2.test/state" in url:
            return httpx.Response(200, json={"tree_size": 0, "root_hash": ""})
        if "w1.test/cosign" in url:
            return httpx.Response(200, json={"signature": sig1})
        if "w2.test/cosign" in url:
            return httpx.Response(200, json={"signature": sig2})
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)

    with patch.object(settings, "PRIVACY_WITNESSES", json.dumps(config_witnesses)), \
         patch.object(settings, "PRIVACY_WITNESS_THRESHOLD", 2):
        cosigs = await gather_witness_cosignatures(checkpoint, db=db_session, tree_size=1, client=client)
        assert len(cosigs) == 2
        names = {c["witness"] for c in cosigs}
        assert names == {"w1", "w2"}


@pytest.mark.asyncio
async def test_gather_witness_cosignatures_threshold_not_met(db_engine):
    sm = get_sessionmaker()
    async with sm() as db_session:
        w1_priv, w1_pub = generate_ed25519_keypair()
        log_pub = "11" * 32
        root = "22" * 32
        checkpoint = {"tree_size": 1, "root_hash": root, "log_public_key": log_pub}

        config_witnesses = {
            "w1": {"url": "http://w1.test", "public_key": w1_pub},
            "w2": {"url": "http://w2.test", "public_key": "33" * 32},
        }

        async def mock_handler(request: httpx.Request):
            url = str(request.url)
            if "w1.test/state" in url:
                return httpx.Response(500)  # Down
            if "w2.test/state" in url:
                return httpx.Response(500)  # Down
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_handler)
        client = httpx.AsyncClient(transport=transport)

        with patch.object(settings, "PRIVACY_WITNESSES", json.dumps(config_witnesses)), \
             patch.object(settings, "PRIVACY_WITNESS_THRESHOLD", 1):
            with pytest.raises(WitnessThresholdNotMetError) as exc_info:
                await gather_witness_cosignatures(checkpoint, db=db_session, tree_size=1, client=client)
            assert "insufficient witness cosignatures" in str(exc_info.value)


@pytest.mark.asyncio
async def test_gather_witness_cosignatures_bad_signature_ignored(db_engine):
    sm = get_sessionmaker()
    async with sm() as db_session:
        w1_priv, w1_pub = generate_ed25519_keypair()
        other_priv, _ = generate_ed25519_keypair()
        log_pub = "11" * 32
        root = "22" * 32
        checkpoint = {"tree_size": 1, "root_hash": root, "log_public_key": log_pub}

        stmt1 = canonical_json({
            "v": "ccpp-witness-v1",
            "tree_size": 1,
            "root_hash": root.lower(),
            "log_public_key": log_pub.lower(),
            "witness": "w1",
        })
        forged_sig = sign(other_priv, stmt1)

        config_witnesses = {
            "w1": {"url": "http://w1.test", "public_key": w1_pub},
        }

        async def mock_handler(request: httpx.Request):
            url = str(request.url)
            if "w1.test/state" in url:
                return httpx.Response(200, json={"tree_size": 0, "root_hash": ""})
            if "w1.test/cosign" in url:
                return httpx.Response(200, json={"signature": forged_sig})
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_handler)
        client = httpx.AsyncClient(transport=transport)

        with patch.object(settings, "PRIVACY_WITNESSES", json.dumps(config_witnesses)), \
             patch.object(settings, "PRIVACY_WITNESS_THRESHOLD", 1):
            with pytest.raises(WitnessThresholdNotMetError):
                await gather_witness_cosignatures(checkpoint, db=db_session, tree_size=1, client=client)


@pytest.mark.asyncio
async def test_gather_witness_cosignatures_ahead_skipped(db_engine):
    sm = get_sessionmaker()
    async with sm() as db_session:
        w1_priv, w1_pub = generate_ed25519_keypair()
        log_pub = "11" * 32
        root = "22" * 32
        checkpoint = {"tree_size": 1, "root_hash": root, "log_public_key": log_pub}

        config_witnesses = {
            "w1": {"url": "http://w1.test", "public_key": w1_pub},
        }

        async def mock_handler(request: httpx.Request):
            url = str(request.url)
            if "w1.test/state" in url:
                # Witness has seen tree size 5, but our checkpoint is size 1
                return httpx.Response(200, json={"tree_size": 5, "root_hash": "44" * 32})
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_handler)
        client = httpx.AsyncClient(transport=transport)

        with patch.object(settings, "PRIVACY_WITNESSES", json.dumps(config_witnesses)), \
             patch.object(settings, "PRIVACY_WITNESS_THRESHOLD", 1):
            with pytest.raises(WitnessThresholdNotMetError):
                await gather_witness_cosignatures(checkpoint, db=db_session, tree_size=1, client=client)


@pytest.mark.asyncio
async def test_get_proofs_at_size_includes_witness_cosignatures(db_engine):
    from app.services.transparency_log import append_transparency_log_entry, get_proofs_at_size

    sm = get_sessionmaker()
    async with sm() as db:
        await append_transparency_log_entry(db, "IMAGE_HASH", {"test": "data"})
        await db.flush()

        w_priv, w_pub = generate_ed25519_keypair()
        config_witnesses = {
            "w1": {"url": "http://w1.test", "public_key": w_pub},
        }

        async def mock_handler(request: httpx.Request):
            url = str(request.url)
            if "w1.test/state" in url:
                return httpx.Response(200, json={"tree_size": 0, "root_hash": ""})
            if "w1.test/cosign" in url:
                body = json.loads(request.content)
                cp = body["checkpoint"]
                stmt = canonical_json({
                    "v": "ccpp-witness-v1",
                    "tree_size": cp["tree_size"],
                    "root_hash": cp["root_hash"].lower(),
                    "log_public_key": cp["log_public_key"].lower(),
                    "witness": "w1",
                })
                return httpx.Response(200, json={"signature": sign(w_priv, stmt)})
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_handler)
        with patch.object(settings, "PRIVACY_WITNESSES", json.dumps(config_witnesses)), \
             patch.object(settings, "PRIVACY_WITNESS_THRESHOLD", 1), \
             patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            res = await get_proofs_at_size(db, leaf_index=0, tree_size=1)
            checkpoint = res["checkpoint"]
            assert "witness_cosignatures" in checkpoint
            assert len(checkpoint["witness_cosignatures"]) == 1
            assert checkpoint["witness_cosignatures"][0]["witness"] == "w1"




# --------------------------------------------------------------------------- against the real witness service

import pathlib  # noqa: E402
import sys  # noqa: E402

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:  # the witness service lives at <repo>/apps/witness
    sys.path.insert(0, _REPO_ROOT)

import apps.witness.main as witness_main  # noqa: E402
from apps.witness.config import WitnessConfig  # noqa: E402


class _WitnessNet(httpx.AsyncBaseTransport):
    """Routes http://<name>.test/* to a real apps.witness instance with that name's config."""

    def __init__(self, tmp_path, names, down=()):
        from app.services.privacy_keys import log_public_key

        self.configs, self.pubs, self.down = {}, {}, set(down)
        for name in names:
            priv, pub = generate_ed25519_keypair()
            self.pubs[name] = pub
            self.configs[f"{name}.test"] = WitnessConfig(
                name=name, private_key=priv, log_public_key=log_public_key(), state_path=str(tmp_path / f"{name}.json")
            )

    def settings_json(self):
        return json.dumps({n: {"url": f"http://{n}.test", "public_key": p} for n, p in self.pubs.items()})

    async def handle_async_request(self, request):
        if request.url.host.removesuffix(".test") in self.down:
            raise httpx.ConnectError("witness down")
        witness_main.config = self.configs[request.url.host]
        return await httpx.ASGITransport(app=witness_main.app).handle_async_request(request)


def _real_witnesses(monkeypatch, tmp_path, names=("w1", "w2"), threshold=2, down=()):
    net = _WitnessNet(tmp_path, names, down)
    monkeypatch.setattr(settings, "PRIVACY_WITNESSES", net.settings_json())
    monkeypatch.setattr(settings, "PRIVACY_WITNESS_THRESHOLD", threshold)
    # Only the witness client is rerouted (patching httpx.AsyncClient itself would reroute every client).
    import types

    import app.services.log_witness as log_witness

    monkeypatch.setattr(log_witness, "httpx", types.SimpleNamespace(AsyncClient=lambda **kw: httpx.AsyncClient(transport=net)))
    return net


@pytest.mark.asyncio
async def test_worker_witnesses_and_an_older_receipt_gets_a_later_checkpoint(db_engine, monkeypatch, tmp_path):
    """E4: the worker has the latest checkpoint cosigned and stores it; a receipt made at an
    earlier size gets that checkpoint plus a consistency proof from its own tree."""
    from app.services.log_witness import build_witness_section_for_receipt, run_witness_cycle, witness_statement
    from app.services.privacy_crypto import verify
    from app.services.transparency_log import append_transparency_log_entry, verify_consistency_proof

    net = _real_witnesses(monkeypatch, tmp_path)
    sm = get_sessionmaker()
    async with sm() as db:
        for i in range(2):
            receipt_leaf = await append_transparency_log_entry(db, "IMAGE_HASH", {"i": i})
        await db.commit()
        assert await build_witness_section_for_receipt(db, 2) is None  # nothing witnessed yet
        for i in range(2, 5):
            await append_transparency_log_entry(db, "IMAGE_HASH", {"i": i})
        await db.commit()

        res = await run_witness_cycle(db)
        assert res["witnessed"] is True and res["size"] == 5 and sorted(res["witnesses"]) == ["w1", "w2"]
        assert (await run_witness_cycle(db))["status"] == "already_witnessed"  # no second round

        section = await build_witness_section_for_receipt(db, 2)
    cp = section["checkpoint"]
    assert cp["tree_size"] == 5
    assert verify_consistency_proof(2, 5, receipt_leaf.merkle_root, cp["root_hash"], section["consistency_proof"])
    for cs in section["cosignatures"]:
        stmt = witness_statement(5, cp["root_hash"], cp["log_public_key"], cs["witness"])
        assert verify(net.pubs[cs["witness"]], stmt, cs["signature"])
    assert {c["witness"] for c in section["cosignatures"]} == {"w1", "w2"}


@pytest.mark.asyncio
async def test_witness_cycle_below_threshold_stores_nothing(db_engine, monkeypatch, tmp_path):
    from app.models.privacy import TransparencyLogLeaf
    from app.services.log_witness import run_witness_cycle
    from app.services.transparency_log import append_transparency_log_entry
    from sqlalchemy import select

    _real_witnesses(monkeypatch, tmp_path, down=("w2",))
    sm = get_sessionmaker()
    async with sm() as db:
        await append_transparency_log_entry(db, "IMAGE_HASH", {"i": 0})
        await db.commit()
        with pytest.raises(WitnessThresholdNotMetError):
            await run_witness_cycle(db)
        leaf = (await db.execute(select(TransparencyLogLeaf))).scalar_one()
        assert leaf.witness_cosignatures is None


@pytest.mark.asyncio
async def test_key_release_reports_too_few_witnesses_as_a_guardian_error(db_engine, monkeypatch, tmp_path):
    """Too few witnesses during an external key release used to escape as a bare exception (HTTP 500,
    job stuck). It must be a GuardianReleaseError so the job fails cleanly with its hold refunded."""
    from app.services.key_guardians import GuardianReleaseError, _release_external
    from app.services.transparency_log import append_transparency_log_entry

    _real_witnesses(monkeypatch, tmp_path, down=("w1", "w2"))
    sm = get_sessionmaker()
    async with sm() as db:
        leaf = await append_transparency_log_entry(db, "KEY_RELEASE", {"guardian": "partner"})
        await db.commit()

        def guardian(request):
            return httpx.Response(200, json={"tree_size": 0, "root_hash": None})

        factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(guardian))  # noqa: E731
        item = {"log_leaf_index": leaf.leaf_index, "log_payload": leaf.payload}
        with pytest.raises(GuardianReleaseError) as exc:
            await _release_external(db, factory, "http://g.test", "partner", type("J", (), {"id": "j"}),
                                    type("M", (), {"id": "m"}), {}, {}, item)
    assert exc.value.code == "witness_threshold_not_met"


@pytest.mark.asyncio
async def test_external_release_retries_when_a_concurrent_release_moved_the_guardian_ahead(db_engine, monkeypatch):
    """Two releases at once: the later (larger) checkpoint can reach the guardian first, which then
    refuses the earlier one as a rollback. That is retried with fresh state; a real inconsistency is not."""
    from app.services.key_guardians import GuardianReleaseError, _release_external
    from app.services.transparency_log import append_transparency_log_entry

    monkeypatch.setattr(settings, "PRIVACY_WITNESS_THRESHOLD", 0)
    sm = get_sessionmaker()
    async with sm() as db:
        leaf = await append_transparency_log_entry(db, "KEY_RELEASE", {"guardian": "partner"})
        await db.commit()
        item = {"log_leaf_index": leaf.leaf_index, "log_payload": leaf.payload}
        job, machine = type("J", (), {"id": "j"}), type("M", (), {"id": "m"})

        def run(answers):
            posts = []

            def guardian(request):
                if request.url.path == "/state":
                    return httpx.Response(200, json={"tree_size": 0, "root_hash": None})
                posts.append(1)
                status, body = answers[min(len(posts), len(answers)) - 1]
                return httpx.Response(status, json=body)

            return posts, (lambda: httpx.AsyncClient(transport=httpx.MockTransport(guardian)))

        posts, factory = run([(409, {"detail": "log_rolled_back"}), (200, {"wrapped_share": {"ok": 1}})])
        assert await _release_external(db, factory, "http://g.test", "partner", job, machine, {}, {}, item) == {"ok": 1}
        assert len(posts) == 2

        posts, factory = run([(409, {"detail": "log_inconsistent"})])
        with pytest.raises(GuardianReleaseError) as exc:
            await _release_external(db, factory, "http://g.test", "partner", job, machine, {}, {}, item)
        assert exc.value.code == "external_guardian_refused:log_inconsistent" and len(posts) == 1


def test_boot_guard_counts_distinct_witness_keys():
    """Threshold 2 with one key listed under two names is one witness: refused outside dev."""
    from app.core.config import Settings

    base = dict(
        ENVIRONMENT="production", AUTH_SECRET="strong-secret-value-for-production-environment-1234",
        SESSION_SECRET="strong-session-secret-value-for-production-1234", SETTLEMENT_ENABLED=False,
        PRIVACY_ENABLED=True, PRIVACY_WITNESS_THRESHOLD=2,
    )
    same = json.dumps({"a": {"url": "https://a.example", "public_key": "aa" * 32}, "b": {"url": "https://b.example", "public_key": "AA" * 32}})
    with pytest.raises(ValueError) as exc:
        Settings(**base, PRIVACY_WITNESSES=same)
    assert "PRIVACY_WITNESSES" in str(exc.value)
