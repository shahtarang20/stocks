"""Attacker-supplied strings reach constant-time comparisons. ``hmac.compare_digest`` raises ``TypeError`` for a
non-ASCII ``str``, which surfaced as a 500; each of these must reject the value cleanly instead."""

from __future__ import annotations

import base64
import datetime as dt
import uuid

import pytest

from app.services import datasets, multi_node, webhooks, wallet
from app.services.stripe_payment import StripeSignatureError, verify_stripe_signature


def test_dataset_grant_with_non_ascii_signature_is_refused_not_a_crash():
    exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
    raw = f"dataset:{uuid.uuid4()}:{exp}:é-not-ascii"
    token = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    assert datasets.verify_grant("dataset", token) is None


def test_multi_node_nonce_with_non_ascii_mac_is_refused():
    import time

    now = int(time.time())
    prober, peer = uuid.uuid4(), uuid.uuid4()
    good = multi_node.make_nonce(prober, peer, now + 30)
    assert multi_node.nonce_ok(good, prober, peer, now) is True
    exp = good.split(".", 1)[0]
    assert multi_node.nonce_ok(f"{exp}.é", prober, peer, now) is False


def test_webhook_signature_with_non_ascii_value_is_false():
    assert webhooks.verify_signature("whsec_x", "1700000000", "{}", "sé") is False


def test_wallet_link_message_with_non_ascii_text_is_rejected_cleanly():
    user_id = uuid.uuid4()
    address = "0x" + "1" * 40
    now = dt.datetime.now(dt.timezone.utc)
    message = wallet.build_message(user_id, address, 56, now)
    with pytest.raises(wallet.WalletLinkError):
        wallet.verify_link(
            user_id=user_id, address=address, chain_id=56, message=message + "é", signature="0x00", now=now
        )


def test_stripe_signature_header_with_non_ascii_v1_is_a_signature_error():
    header = "t=1700000000,v1=é"
    with pytest.raises(StripeSignatureError):
        verify_stripe_signature(b"{}", header, "whsec_test", tolerance=0)
