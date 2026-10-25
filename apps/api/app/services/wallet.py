"""Wallet linking: prove an address belongs to an account, then read its token balance.

The proof is an EIP-191 ``personal_sign`` signature over a message the server issued.
The message names the account, the address, the chain and a timestamp, and carries an
HMAC tag, so the server can check it came from us without storing anything. Because the
account id is inside the signed text and the request needs the account's own session,
a signature made for one account cannot be used to link a wallet to another.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import uuid

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import is_address, to_checksum_address

from app.core.config import settings

_PREFIX = "ComputeCoin wallet link"
_TAG_RE = re.compile(r"^Tag: ([0-9a-f]{64})$", re.M)


class WalletLinkError(ValueError):
    """Raised with a stable error code the API returns to the caller."""


def normalise_address(address: str) -> str:
    if not isinstance(address, str) or not is_address(address):
        raise WalletLinkError("invalid_address")
    return address.lower()


def display_address(address: str) -> str:
    return to_checksum_address(address)


def _tag(body: str) -> str:
    return hmac.new(settings.AUTH_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()


def build_message(user_id: uuid.UUID, address: str, chain_id: int, now: dt.datetime) -> str:
    body = (
        f"{_PREFIX}\n"
        f"Account: {user_id}\n"
        f"Address: {display_address(address)}\n"
        f"Chain: {chain_id}\n"
        f"Issued: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    return f"{body}\nTag: {_tag(body)}"


def verify_link(
    *, user_id: uuid.UUID, address: str, chain_id: int, message: str, signature: str, now: dt.datetime
) -> str:
    """Return the lower-cased address if the signature proves control of it, else raise."""
    addr = normalise_address(address)
    m = _TAG_RE.search(message or "")
    if m is None:
        raise WalletLinkError("message_invalid")
    body = message[: m.start()].rstrip("\n")
    if not hmac.compare_digest(m.group(1), _tag(body)):
        raise WalletLinkError("message_invalid")  # not issued by us, or edited

    expected = build_message(user_id, addr, chain_id, _parse_issued(body))
    if not hmac.compare_digest(expected, message):
        raise WalletLinkError("message_mismatch")  # wrong account, address or chain

    age = (now - _parse_issued(body)).total_seconds()
    if age < -60 or age > settings.WALLET_LINK_MESSAGE_TTL_SECONDS:
        raise WalletLinkError("message_expired")

    try:
        signer = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception:  # noqa: BLE001 - any malformed signature is just "invalid"
        raise WalletLinkError("signature_invalid") from None
    if signer.lower() != addr:
        raise WalletLinkError("signature_wrong_signer")
    return addr


def _parse_issued(body: str) -> dt.datetime:
    m = re.search(r"^Issued: (\S+)$", body, re.M)
    if m is None:
        raise WalletLinkError("message_invalid")
    try:
        return dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        raise WalletLinkError("message_invalid") from None
