"""Private (sealed) chat streaming for the customer (U18, spec ccpp-v1.md section 6g).

The server relays only ciphertext. Each SSE event ``chat.completion.chunk.sealed`` carries one sealed envelope to the
customer's result public key. The plaintext of a chunk is the text piece, padded (when ``PRIVACY_STREAM_PAD_BYTES`` > 0)
as ``u32 big-endian length || UTF-8 text || zero bytes``; the AAD is ``ccpp-v1|stream|<job_id>|<seq>|<final>``.

This module opens and un-pads chunks and checks order, so a reorder, a replay, a chunk from another job and a stream
cut short (no ``final`` chunk) are all errors rather than silently shorter text.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator, Optional

from .privacy_crypto import VERSION, PrivacyCryptoError, open_sealed

DEFAULT_PAD_BYTES = 256


class PrivateStreamError(ValueError):
    """A sealed stream chunk is malformed, out of order, does not open, or the stream is truncated."""


def stream_aad(job_id: str, seq: int, final: bool) -> bytes:
    return f"{VERSION}|stream|{job_id}|{int(seq)}|{1 if final else 0}".encode()


def unpad_stream_plaintext(data: bytes) -> bytes:
    """Inverse of the agent's ``pad_stream_plaintext``: read the u32 BE length, return exactly that many bytes.

    Rejects a length that exceeds the payload and any non-zero padding byte (a tampered or mis-framed chunk)."""
    if len(data) < 4:
        raise PrivateStreamError("padded chunk is shorter than its 4 byte length prefix")
    n = int.from_bytes(data[:4], "big")
    if n > len(data) - 4:
        raise PrivateStreamError("padded chunk length exceeds its payload")
    if any(data[4 + n:]):
        raise PrivateStreamError("padded chunk has non-zero padding bytes")
    return data[4:4 + n]


def open_stream_chunk(
    customer_private_key: str,
    envelope: dict[str, Any],
    *,
    job_id: str,
    seq: int,
    final: bool,
    pad_bytes: int = DEFAULT_PAD_BYTES,
) -> str:
    """Open one sealed chunk with the same AAD the agent used and return its text. ``pad_bytes <= 0`` = unpadded stream."""
    try:
        plain = open_sealed(customer_private_key, envelope, stream_aad(job_id, seq, final))
    except PrivacyCryptoError as exc:
        raise PrivateStreamError(f"stream chunk {seq} does not open: {exc}") from exc
    if pad_bytes > 0:
        plain = unpad_stream_plaintext(plain)
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrivateStreamError(f"stream chunk {seq} is not valid UTF-8") from exc


class PrivateStreamReader:
    """Stateful reader over the parsed SSE events of one private stream. ``feed(event)`` returns the text of that chunk."""

    def __init__(self, customer_private_key: str, *, pad_bytes: int = DEFAULT_PAD_BYTES, job_id: Optional[str] = None) -> None:
        self._key = customer_private_key
        self.pad_bytes = pad_bytes
        self.job_id = job_id  # taken from the first event when not given; later events must match it
        self.next_seq = 0
        self.finished = False

    def feed(self, event: dict[str, Any]) -> str:
        if not isinstance(event, dict):
            raise PrivateStreamError("stream event is not an object")
        err = event.get("error")
        if isinstance(err, dict):
            raise PrivateStreamError(f"stream error: {err.get('code') or err.get('message') or 'unknown'}")
        if event.get("object") != "chat.completion.chunk.sealed":
            raise PrivateStreamError("unexpected (possibly plaintext) event in a private stream")
        if self.finished:
            raise PrivateStreamError("chunk after the final chunk")
        job_id, seq, final = event.get("job_id"), event.get("seq"), event.get("final")
        if not isinstance(job_id, str) or not job_id:
            raise PrivateStreamError("sealed chunk has no job_id")
        if self.job_id is None:
            self.job_id = job_id
        elif job_id != self.job_id:
            raise PrivateStreamError("sealed chunk belongs to another job")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq != self.next_seq:
            raise PrivateStreamError(f"expected chunk {self.next_seq}, got {seq!r}")
        if not isinstance(final, bool):
            raise PrivateStreamError("sealed chunk has no final flag")
        text = open_stream_chunk(self._key, event.get("sealed"), job_id=job_id, seq=seq, final=final, pad_bytes=self.pad_bytes)
        self.next_seq += 1
        self.finished = final
        return text

    def close(self) -> None:
        """Call at the end of the stream: a stream with no ``final`` chunk is truncated."""
        if not self.finished:
            raise PrivateStreamError("sealed stream ended without its final chunk (truncated)")


def iter_sse_data(lines: Iterable[str]) -> Iterator[Any]:
    """Parse ``data: ...`` SSE lines into JSON objects; the ``[DONE]`` sentinel ends the iteration."""
    for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        if payload:
            try:
                yield json.loads(payload)
            except ValueError as exc:
                raise PrivateStreamError("stream event is not JSON") from exc


def iter_private_text(lines: Iterable[str], customer_private_key: str, *, pad_bytes: int = DEFAULT_PAD_BYTES) -> Iterator[str]:
    """Decrypted text deltas from the SSE lines of a private chat stream; raises if the stream is truncated."""
    reader = PrivateStreamReader(customer_private_key, pad_bytes=pad_bytes)
    for event in iter_sse_data(lines):
        yield reader.feed(event)
    reader.close()
