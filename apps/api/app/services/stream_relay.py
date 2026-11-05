"""In-memory relay of ordered token chunks from an agent to a customer's SSE stream (B2).

Process-local on purpose (see docs/architecture/streaming-chat.md): several API workers need a
shared bus before streaming real tokens behind a load balancer. A stream exists only while a
customer request has opened it, so an agent cannot make the API buffer for nobody.

Rules: ``seq`` starts at 0 and must be exactly the next number. An exact repeat of an accepted
chunk is a harmless retry. Anything else (gap, reorder, changed content, too much text) puts the
stream in an error state that the customer stream reports and ends on.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from app.core.config import settings


class StreamSequenceError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Stream:
    next_seq: int = 0
    chars: int = 0
    pieces: list[str] = field(default_factory=list)
    hashes: list[str] = field(default_factory=list)
    final: bool = False
    error: str | None = None
    sealed: bool = False  # U18: a private stream; pieces are sealed-envelope JSON, never plaintext


_streams: dict[str, _Stream] = {}


# --------------------------------------------------------------------------- #
# Redis backend (STREAM_RELAY_BACKEND=redis): the same rules, shared by every API worker.
# Atomicity comes from one Lua script per chunk. ``redis`` is imported lazily so the memory
# backend (the default) never needs it. State expires on its own (TTL) if close_stream is missed.
# --------------------------------------------------------------------------- #
_STREAM_TTL_SECONDS = 3600
_ACCEPT_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then return {0, 0, ''} end
local err = redis.call('HGET', KEYS[1], 'error')
if err and err ~= '' then return {2, 0, err} end
local nxt = tonumber(redis.call('HGET', KEYS[1], 'next_seq'))
local seq = tonumber(ARGV[1])
local function fail(reason) redis.call('HSET', KEYS[1], 'error', reason) return {2, nxt, reason} end
if seq < nxt then
  if redis.call('LINDEX', KEYS[3], seq) == ARGV[3] then return {1, nxt, ''} end
  return fail('reordered_or_modified_chunk')
end
if seq > nxt then return fail('sequence_gap') end
if redis.call('HGET', KEYS[1], 'final') == '1' then return fail('chunk_after_final') end
local chars = tonumber(redis.call('HGET', KEYS[1], 'chars'))
if chars + tonumber(ARGV[5]) > tonumber(ARGV[4]) then return fail('stream_too_large') end
redis.call('RPUSH', KEYS[2], ARGV[2])
redis.call('RPUSH', KEYS[3], ARGV[3])
redis.call('HSET', KEYS[1], 'chars', chars + tonumber(ARGV[5]), 'next_seq', nxt + 1, 'final', ARGV[6])
return {1, nxt + 1, ''}
"""
_redis_client = None


def _backend() -> str:
    return (getattr(settings, "STREAM_RELAY_BACKEND", "memory") or "memory").lower()


def _redis():
    global _redis_client
    if _redis_client is None:
        import redis  # optional dependency, only for STREAM_RELAY_BACKEND=redis

        _redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


def _rkeys(job_id: str) -> list[str]:
    base = f"ccoin:stream:{job_id}"
    return [f"{base}:m", f"{base}:p", f"{base}:h"]


def open_stream(job_id: str, sealed: bool = False) -> None:
    if _backend() == "redis":
        m, p, h = _rkeys(str(job_id))
        r = _redis()
        pipe = r.pipeline()
        pipe.delete(m, p, h)
        pipe.hset(m, mapping={"next_seq": 0, "chars": 0, "final": 0, "error": "", "sealed": 1 if sealed else 0})
        pipe.expire(m, _STREAM_TTL_SECONDS)
        pipe.execute()
        return
    _streams[str(job_id)] = _Stream(sealed=sealed)


def close_stream(job_id: str) -> None:
    if _backend() == "redis":
        _redis().delete(*_rkeys(str(job_id)))
        return
    _streams.pop(str(job_id), None)


def is_open(job_id: str) -> bool:
    if _backend() == "redis":
        return bool(_redis().exists(_rkeys(str(job_id))[0]))
    return str(job_id) in _streams


def is_sealed_stream(job_id: str) -> bool:
    if _backend() == "redis":
        return _redis().hget(_rkeys(str(job_id))[0], "sealed") == "1"
    st = _streams.get(str(job_id))
    return bool(st and st.sealed)


def accept_sealed_chunk(job_id: str, seq: int, envelope: object, final: bool) -> tuple[bool, int]:
    """U18: relay one sealed envelope. Only structure, order and size are checked; the relay holds no key.

    Raises StreamSequenceError("not_a_sealed_stream") for a stream the customer did not open sealed.
    """
    from app.services.privacy_crypto import is_sealed_envelope

    if not is_open(job_id):
        return False, 0
    if not is_sealed_stream(job_id):
        raise StreamSequenceError("not_a_sealed_stream")
    if not is_sealed_envelope(envelope):
        raise StreamSequenceError("invalid_sealed_chunk")
    pad = int(getattr(settings, "PRIVACY_STREAM_PAD_BYTES", 0) or 0)
    if pad > 0:
        # Padded stream: the AEAD ciphertext is plaintext (a multiple of ``pad``) plus the 16 byte tag. A chunk that is not
        # block aligned would leak its length, so it is refused (the relay cannot read it; length is all it can check).
        ct = envelope["ct"]
        if len(ct) % 2 or len(ct) // 2 - 16 < pad or (len(ct) // 2 - 16) % pad != 0:
            raise StreamSequenceError("sealed_chunk_not_padded")
    # The final flag is stored with the envelope: the customer needs it to rebuild each chunk's AAD.
    return accept_chunk(job_id, seq, json.dumps({"final": bool(final), "sealed": envelope}, sort_keys=True, separators=(",", ":")), final)


def _accept_chunk_redis(job_id: str, seq: int, text: str, final: bool) -> tuple[bool, int]:
    m, p, h = _rkeys(str(job_id))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    code, nxt, err = _redis().eval(
        _ACCEPT_LUA, 3, m, p, h, seq, text, digest,
        settings.PRIVACY_STREAM_MAX_CHARS if is_sealed_stream(job_id) else settings.INFERENCE_STREAM_MAX_CHARS, len(text), 1 if final else 0
    )
    if int(code) == 0:
        return False, 0
    if int(code) == 2:
        raise StreamSequenceError(str(err))
    return True, int(nxt)


def accept_chunk(job_id: str, seq: int, text: str, final: bool) -> tuple[bool, int]:
    """Returns (accepted, next_seq). Raises StreamSequenceError after recording the error."""
    if _backend() == "redis":
        return _accept_chunk_redis(job_id, seq, text, final)
    st = _streams.get(str(job_id))
    if st is None:
        return False, 0
    if st.error:
        raise StreamSequenceError(st.error)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if seq < st.next_seq:
        if st.hashes[seq] == digest:
            return True, st.next_seq  # retry of an accepted chunk
        st.error = "reordered_or_modified_chunk"
        raise StreamSequenceError(st.error)
    if seq > st.next_seq:
        st.error = "sequence_gap"
        raise StreamSequenceError(st.error)
    if st.final:
        st.error = "chunk_after_final"
        raise StreamSequenceError(st.error)
    limit = settings.PRIVACY_STREAM_MAX_CHARS if st.sealed else settings.INFERENCE_STREAM_MAX_CHARS
    if st.chars + len(text) > limit:
        st.error = "stream_too_large"
        raise StreamSequenceError(st.error)
    st.pieces.append(text)
    st.hashes.append(digest)
    st.chars += len(text)
    st.next_seq += 1
    st.final = final
    return True, st.next_seq


def read_from(job_id: str, index: int) -> tuple[list[str], bool, str | None]:
    """Pieces at or after ``index``, whether the final chunk arrived, and any error."""
    if _backend() == "redis":
        m, p, _h = _rkeys(str(job_id))
        r = _redis()
        meta = r.hmget(m, "final", "error")
        if meta[0] is None and meta[1] is None:
            return [], False, None
        return list(r.lrange(p, index, -1)), meta[0] == "1", (meta[1] or None)
    st = _streams.get(str(job_id))
    if st is None:
        return [], False, None
    return st.pieces[index:], st.final, st.error
