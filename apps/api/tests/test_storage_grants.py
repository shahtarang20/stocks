"""Signed artifact grants — every REJECTION branch.

Artifact upload and download are authorized by a bearer token rather than a
session: whoever holds a valid grant may write a job's artifact or read one.
The only thing standing between a URL and someone else's data is
``_verify_grant``: scope, HMAC signature and expiry.

The happy path was already exercised indirectly by the artifact endpoints, so
uploads and downloads working proves nothing about the checks. Every branch
below is a rejection, and each one is a real attack:

  - lengthening your own grant's expiry
  - pointing a grant at a different job or artifact
  - replaying an upload token against the download route
  - forging a signature outright

Without these tests, deleting any single check leaves the suite green.
"""

from __future__ import annotations

import base64
import datetime as dt

import pytest

from app.db.base import utcnow
from app.services.storage import (
    _make_grant,
    _sign,
    make_download_grant,
    make_upload_grant,
    verify_download_grant,
    verify_upload_grant,
)

JOB_ID = "11111111-1111-1111-1111-111111111111"
ARTIFACT_ID = "22222222-2222-2222-2222-222222222222"


def _future(seconds: int = 300) -> dt.datetime:
    return utcnow() + dt.timedelta(seconds=seconds)


def _decode(token: str) -> str:
    return base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")


def _encode(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# happy path — so the rejections below are meaningful
# ---------------------------------------------------------------------------
def test_a_valid_upload_grant_round_trips():
    token = make_upload_grant(JOB_ID, _future())
    assert verify_upload_grant(token) == JOB_ID


def test_a_valid_download_grant_round_trips():
    token = make_download_grant(ARTIFACT_ID, _future())
    assert verify_download_grant(token) == ARTIFACT_ID


# ---------------------------------------------------------------------------
# signature
# ---------------------------------------------------------------------------
def test_a_forged_signature_is_rejected():
    """Hand-built token with a made-up signature."""
    exp = int(_future().timestamp())
    forged = _encode(f"upload:{JOB_ID}:{exp}:" + "a" * 64)
    assert verify_upload_grant(forged) is None


def test_tampering_with_the_resource_id_is_rejected():
    """Re-point a legitimately-issued grant at someone else's job."""
    token = make_upload_grant(JOB_ID, _future())
    scope, _rid, exp, sig = _decode(token).split(":")
    other = "33333333-3333-3333-3333-333333333333"
    assert verify_upload_grant(_encode(f"{scope}:{other}:{exp}:{sig}")) is None


def test_extending_the_expiry_is_rejected():
    """The highest-value attack: make your own short grant last forever.

    The expiry is INSIDE the signed payload, so editing it invalidates the
    signature. If the signature check were dropped, this would succeed and
    every grant would effectively be permanent.
    """
    token = make_upload_grant(JOB_ID, _future(60))
    scope, rid, _exp, sig = _decode(token).split(":")
    far_future = int((utcnow() + dt.timedelta(days=3650)).timestamp())
    assert verify_upload_grant(_encode(f"{scope}:{rid}:{far_future}:{sig}")) is None


def test_a_signature_from_a_different_payload_is_rejected():
    """A real signature, but for different contents — splicing must not work."""
    exp = int(_future().timestamp())
    victim = "44444444-4444-4444-4444-444444444444"
    stolen_sig = _sign(f"upload:{victim}:{exp}")
    assert verify_upload_grant(_encode(f"upload:{JOB_ID}:{exp}:{stolen_sig}")) is None
    # ...and it IS a valid signature for its own payload, so the test is about
    # binding, not about the signature being malformed.
    assert verify_upload_grant(_encode(f"upload:{victim}:{exp}:{stolen_sig}")) == victim


# ---------------------------------------------------------------------------
# scope confusion
# ---------------------------------------------------------------------------
def test_an_upload_grant_cannot_be_used_for_download():
    token = make_upload_grant(JOB_ID, _future())
    assert verify_upload_grant(token) == JOB_ID
    assert verify_download_grant(token) is None


def test_a_download_grant_cannot_be_used_for_upload():
    token = make_download_grant(ARTIFACT_ID, _future())
    assert verify_download_grant(token) == ARTIFACT_ID
    assert verify_upload_grant(token) is None


def test_rewriting_the_scope_is_rejected():
    """Editing the scope breaks the signature, because scope is signed."""
    token = make_download_grant(ARTIFACT_ID, _future())
    _scope, rid, exp, sig = _decode(token).split(":")
    assert verify_upload_grant(_encode(f"upload:{rid}:{exp}:{sig}")) is None


# ---------------------------------------------------------------------------
# expiry
# ---------------------------------------------------------------------------
def test_an_expired_grant_is_rejected():
    token = make_upload_grant(JOB_ID, utcnow() - dt.timedelta(seconds=1))
    assert verify_upload_grant(token) is None


def test_a_grant_is_rejected_one_second_past_expiry():
    expires = _future(60)
    token = make_upload_grant(JOB_ID, expires)
    assert verify_upload_grant(token, now=expires - dt.timedelta(seconds=1)) == JOB_ID
    assert verify_upload_grant(token, now=expires + dt.timedelta(seconds=1)) is None


def test_a_non_numeric_expiry_is_rejected():
    """Signed, but the expiry is not an integer — must not crash or pass."""
    rid, exp_str = JOB_ID, "not-a-number"
    sig = _sign(f"upload:{rid}:{exp_str}")
    assert verify_upload_grant(_encode(f"upload:{rid}:{exp_str}:{sig}")) is None


# ---------------------------------------------------------------------------
# malformed input
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "token,why",
    [
        ("", "empty"),
        ("!!!not-base64!!!", "not base64"),
        (_encode("upload:only:three"), "too few parts"),
        (_encode("upload:a:b:c:d"), "too many parts"),
        (_encode("garbage"), "no separators"),
    ],
)
def test_malformed_tokens_are_rejected(token, why):
    assert verify_upload_grant(token) is None, why
    assert verify_download_grant(token) is None, why


def test_non_utf8_bytes_are_rejected():
    """Valid base64 that does not decode as UTF-8 must not raise."""
    token = base64.urlsafe_b64encode(b"\xff\xfe\xfd\xfc").decode("ascii")
    assert verify_upload_grant(token) is None


def test_grant_is_opaque_and_carries_no_readable_secret():
    """The token embeds the resource id, so it must never also leak the key."""
    from app.core.config import settings

    token = make_upload_grant(JOB_ID, _future())
    assert settings.AUTH_SECRET not in _decode(token)
    assert settings.AUTH_SECRET not in token


def test_two_scopes_never_produce_the_same_token_for_one_id():
    """Scope is part of the signed payload, so the tokens must differ."""
    exp = _future()
    assert _make_grant("upload", JOB_ID, exp) != _make_grant("download", JOB_ID, exp)
