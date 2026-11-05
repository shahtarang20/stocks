"""U10 HTTP bridge: the container app is untrusted, so its response framing must be bounded and strictly parsed."""

import asyncio

import pytest

from computecoin_agent import session_http as sh


class _Reader:
    def __init__(self, parts):
        self.parts = list(parts)
        self.read_bytes = 0

    async def read(self, n):
        if not self.parts:
            return b""
        d = self.parts.pop(0)
        self.read_bytes += len(d)
        return d


async def _collect(reader, headers, rest=b""):
    return [c async for c in sh._body_chunks(reader, headers, 200, "GET", rest)]


def test_chunked_body_round_trip():
    r = _Reader([b"5\r\nhello\r\n", b"6\r\n world\r\n0\r\n\r\n"])
    out = asyncio.run(_collect(r, [("Transfer-Encoding", "chunked")]))
    assert b"".join(out) == b"hello world"


def test_endless_chunk_size_line_is_refused_not_buffered():
    """A chunk-size line that never ends used to grow the agent's buffer without bound (host memory DoS by the app)."""
    r = _Reader([b"A" * 4096 for _ in range(2000)])
    with pytest.raises(sh.HttpBridgeError):
        asyncio.run(_collect(r, [("Transfer-Encoding", "chunked")]))
    assert r.read_bytes < 64 * 1024


def test_oversized_content_length_refused():
    with pytest.raises(sh.HttpBridgeError):
        asyncio.run(_collect(_Reader([]), [("Content-Length", str(sh.MAX_RES_BODY + 1))]))


def test_request_headers_are_allowlisted_and_crlf_refused():
    assert sh.clean_request_headers([["Authorization", "x"], ["Accept", "a"]]) == [("accept", "a")]
    with pytest.raises(sh.HttpBridgeError):
        sh.clean_request_headers([["Accept", "a\r\nX-Evil: 1"]])


def test_service_agent_safe_path_refuses_trailing_newline():
    from computecoin_agent.service_agent import safe_path

    assert safe_path("/ok/path")
    assert not safe_path("/ok\n")
    assert not safe_path("/ok\r\nHost: x")
