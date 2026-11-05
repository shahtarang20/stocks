"""Private Kit input and private fan-out for the customer (U18, spec ccpp-v1.md section 6g "Private Kit input").

The decrypted input a private Kit part receives is::

    {"inputs": {name: {"b64": "<base64>"} | {"text": "<utf-8>"}}, "params": {...}}

Everything is encrypted on this machine (ccpp-v1 data envelope, fresh Data Key per part, Data Key split across the
guardians). Every part gets its OWN Data Key, guardian shares and result keypair: the API refuses a key set reused across
parts. Keep the returned result private keys; they are the only way to open the parts' sealed outputs.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Mapping, Optional, Sequence, Union

from .crypto import generate_customer_keypair, prepare_private_job

_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

InputValue = Union[bytes, bytearray, str, Mapping[str, str]]


def build_private_kit_input(
    inputs: Optional[Mapping[str, InputValue]] = None, params: Optional[Mapping[str, Any]] = None
) -> dict[str, Any]:
    """Build the plaintext of one private Kit part.

    ``inputs`` values: ``bytes`` become ``{"b64": ...}``, ``str`` becomes ``{"text": ...}``, and an already shaped
    ``{"b64": ...}`` / ``{"text": ...}`` is checked and passed through. Names must match ``[A-Za-z0-9._-]{1,128}``
    (they become ``/in/<name>`` in the Kit container)."""
    out: dict[str, dict[str, str]] = {}
    for name, value in (inputs or {}).items():
        if not isinstance(name, str) or not _NAME.match(name):
            raise ValueError(f"bad input name {name!r}: use 1-128 of A-Z a-z 0-9 . _ -")
        if isinstance(value, (bytes, bytearray)):
            out[name] = {"b64": base64.b64encode(bytes(value)).decode("ascii")}
        elif isinstance(value, str):
            out[name] = {"text": value}
        elif isinstance(value, Mapping) and len(value) == 1 and isinstance(value.get("b64"), str):
            try:
                base64.b64decode(value["b64"], validate=True)
            except ValueError as exc:
                raise ValueError(f"input {name!r}: b64 is not valid base64") from exc
            out[name] = {"b64": value["b64"]}
        elif isinstance(value, Mapping) and len(value) == 1 and isinstance(value.get("text"), str):
            out[name] = {"text": value["text"]}
        else:
            raise ValueError(f"input {name!r} must be bytes, str, {{'b64': str}} or {{'text': str}}")
    return {"inputs": out, "params": dict(params or {})}


def prepare_private_fanout_parts(
    plaintexts: Sequence[Mapping[str, Any]],
    *,
    guardians: dict[str, str],
    threshold: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """One ``private_parts`` entry per plaintext (from :func:`build_private_kit_input`) plus the matching result private
    keys, in the same order. A fresh result keypair, Data Key and share set is made for every part."""
    if not plaintexts:
        raise ValueError("a private fan-out needs at least one part")
    parts: list[dict[str, Any]] = []
    private_keys: list[str] = []
    for plain in plaintexts:
        if not isinstance(plain, Mapping) or set(plain) - {"inputs", "params"}:
            raise ValueError("each part plaintext must be {'inputs': ..., 'params': ...} (see build_private_kit_input)")
        priv, pub = generate_customer_keypair()
        prepared = prepare_private_job(dict(plain), guardians=guardians, threshold=threshold, result_public_key=pub)
        parts.append({
            "encrypted_input": prepared["encrypted_input"],
            "dk_shares": prepared["dk_shares"],
            "result_public_key": pub,
        })
        private_keys.append(priv)
    return parts, private_keys
