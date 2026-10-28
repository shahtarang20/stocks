"""Guardian key pinning (spec 6e): the client refuses a guardian set other than the published one."""

from __future__ import annotations

import json
import warnings

import httpx
import pytest

from computecoin import ComputeCoinClient, GuardianPinError, UnpinnedGuardianWarning, guardian_manifest_fingerprint
from computecoin.crypto import generate_customer_keypair
from computecoin.privacy_crypto import generate_x25519_keypair

# Shared vector: the API (app.services.privacy_keys) and the website (ccpp.ts) must produce the same.
VECTOR_GUARDIANS = {"computecoin": "AA" * 32, "partner": "bb" * 32}
VECTOR_FINGERPRINT = "90047f24b306726940afab8f77365e85bf3b89f88bd916a9c5e60da4890a31f1"


def test_fingerprint_vector_and_sensitivity():
    assert guardian_manifest_fingerprint(VECTOR_GUARDIANS, 2) == VECTOR_FINGERPRINT
    assert guardian_manifest_fingerprint({"computecoin": "aa" * 32, "partner": "bb" * 32}, 2) == VECTOR_FINGERPRINT  # case-insensitive keys
    assert guardian_manifest_fingerprint(VECTOR_GUARDIANS, 3) != VECTOR_FINGERPRINT  # threshold is bound in
    assert guardian_manifest_fingerprint({**VECTOR_GUARDIANS, "extra": "cc" * 32}, 2) != VECTOR_FINGERPRINT


def _platform(guardians, posted):
    def handle(request):
        if request.url.path == "/v1/privacy/verifier-key":
            return httpx.Response(200, json={"verifier_public_key": "11" * 32, "guardians": guardians, "threshold": 2})
        posted.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "00000000-0000-0000-0000-000000000001", "status": "queued", "workload_type": "text-generation",
                                         "model": "m", "isolation_tier": "sealed", "created_at": "2026-10-28T00:00:00Z"})
    return httpx.MockTransport(handle)


def _submit(client):
    return client.submit_private_job(workload_type="text-generation", model="m", input_data={"prompt": "secret"},
                                     result_public_key=generate_customer_keypair()[1])


@pytest.fixture
def honest():
    return {"computecoin": generate_x25519_keypair()[1], "partner": generate_x25519_keypair()[1]}


def test_a_platform_serving_other_guardian_keys_is_refused(honest):
    pin = guardian_manifest_fingerprint(honest, 2)
    swapped = {"computecoin": generate_x25519_keypair()[1], "partner": honest["partner"]}  # one key the platform controls
    posted: list = []
    client = ComputeCoinClient("http://api", access_token="t", transport=_platform(swapped, posted), guardian_pin=pin)
    with pytest.raises(GuardianPinError):
        _submit(client)
    assert posted == []  # nothing was encrypted to the wrong keys or sent


def test_the_published_guardian_set_is_accepted(honest):
    posted: list = []
    client = ComputeCoinClient("http://api", access_token="t", transport=_platform(honest, posted),
                               guardian_pin=guardian_manifest_fingerprint(honest, 2))
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no unpinned warning
        _submit(client)
    assert len(posted) == 1 and set(posted[0]["dk_shares"]) == set(honest)


def test_unpinned_warns_and_strict_refuses(honest, monkeypatch):
    monkeypatch.delenv("COMPUTECOIN_GUARDIAN_PIN", raising=False)
    posted: list = []
    with pytest.warns(UnpinnedGuardianWarning):
        _submit(ComputeCoinClient("http://api", access_token="t", transport=_platform(honest, posted)))
    with pytest.raises(GuardianPinError):
        _submit(ComputeCoinClient("http://api", access_token="t", transport=_platform(honest, posted), require_guardian_pin=True))
    assert len(posted) == 1


def test_pin_from_the_environment(honest, monkeypatch):
    monkeypatch.setenv("COMPUTECOIN_GUARDIAN_PIN", "00" * 32)
    with pytest.raises(GuardianPinError):
        _submit(ComputeCoinClient("http://api", access_token="t", transport=_platform(honest, [])))
