"""Signed runtime manifest: signature, digest pins and strict schema (written, never run)."""

from __future__ import annotations

import base64
import json

import pytest

from computecoin_agent.windows.runtime.manifest import (
    ManifestError, SignatureError, decode_public_key, load_verified_manifest,
)

from .fakes import Env, envelope, manifest_dict, new_keypair


def test_valid_manifest_loads():
    priv, pub = new_keypair()
    m = load_verified_manifest(envelope(manifest_dict(), priv), pub)
    assert m.sequence == 5 and m.docker_engine_version == "27.3.1" and m.nvidia_repo.component is None


def test_tampered_payload_fails_signature():
    priv, pub = new_keypair()
    env = json.loads(envelope(manifest_dict(), priv))
    payload = bytearray(base64.b64decode(env["payload"]))
    payload[payload.index(b"2026")] ^= 0x01  # change one signed byte
    env["payload"] = base64.b64encode(bytes(payload)).decode()
    with pytest.raises(SignatureError):
        load_verified_manifest(json.dumps(env).encode(), pub)


def test_signature_from_another_key_fails():
    priv, _pub = new_keypair()
    _other_priv, other_pub = new_keypair()
    with pytest.raises(SignatureError):
        load_verified_manifest(envelope(manifest_dict(), priv), other_pub)


@pytest.mark.parametrize("pin", ["", "   ", "AAAA", "00" * 32, "not base64 !!"])
def test_missing_or_malformed_pinned_key_refuses_everything(pin):
    priv, _pub = new_keypair()
    with pytest.raises(SignatureError):
        load_verified_manifest(envelope(manifest_dict(), priv), pin)


def test_unsigned_envelope_refused():
    priv, pub = new_keypair()
    env = json.loads(envelope(manifest_dict(), priv))
    env["signature"] = ""
    with pytest.raises(ManifestError):
        load_verified_manifest(json.dumps(env).encode(), pub)


def test_unsigned_field_next_to_signed_bytes_is_never_read():
    """The rootfs digest comes only from the signed payload, never from a top-level field beside it."""
    priv, pub = new_keypair()
    env = json.loads(envelope(manifest_dict(), priv))
    env["rootfs_sha256"] = "f" * 64
    env["rootfs"] = {"url": "https://evil.example/x", "sha256": "f" * 64}
    m = load_verified_manifest(json.dumps(env).encode(), pub)
    assert m.rootfs_sha256 != "f" * 64 and m.rootfs_url == "https://dl.example/rootfs.tar.gz"


def test_hex_public_key_is_accepted():
    _priv, pub = new_keypair()
    assert decode_public_key(base64.b64decode(pub).hex()) == base64.b64decode(pub)


@pytest.mark.parametrize("mutate", [
    lambda d: d["rootfs"].update(url="http://dl.example/rootfs.tar.gz"),
    lambda d: d["rootfs"].update(sha256="ABC"),
    lambda d: d["smoke_test"].update(image="registry.example/cuda:latest"),
    lambda d: d["egress_proxy"].update(image="registry.example/proxy:1"),
    lambda d: d["docker"]["packages"].update({"docker-ce": "--allow-unauthenticated"}),
    lambda d: d["docker"]["packages"].update({"evil-pkg": "1.0"}),
    lambda d: d["docker"]["repo"].update(key_fingerprint="1234"),
    lambda d: d["runner"].update(wheel_filename="../../x.whl"),
    lambda d: d.update(arch="arm64"),
    lambda d: d.update(schema=2),
    lambda d: d["nvidia_toolkit"]["repo"].update(component="main"),  # a flat repo has no component
])
def test_schema_violations_refused(mutate):
    priv, pub = new_keypair()
    d = manifest_dict()
    mutate(d)
    with pytest.raises(ManifestError):
        load_verified_manifest(envelope(d, priv), pub)


def test_bad_signature_means_no_wsl_command_runs(tmp_path):
    env = Env(tmp_path)
    other_priv, _ = new_keypair()
    res = env.prov.provision(envelope(manifest_dict(), other_priv))
    assert not res.ok and "manifest refused" in res.error
    assert env.fake.calls == [] and env.fetcher.calls == []
