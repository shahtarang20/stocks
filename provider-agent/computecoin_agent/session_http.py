"""U10 HTTP-over-encrypted-channel bridge (host side). See docs/architecture/U10-interactive-sessions-design.md section 9.

The browser sends HTTP request frames inside the end-to-end encrypted session channel (plaintext type 4). For each
request this module opens ONE short-lived `docker exec -i <container> nc 127.0.0.1 <tcp_port>` stream (the container has
no network and publishes no port), writes a plain HTTP/1.1 request, parses the response and streams it back as frames.
A WebSocket upgrade becomes a raw binary tunnel after the 101 response.

Everything the browser sends is treated as untrusted: method, path and header names are allowlisted, header values
cannot carry CR/LF, sizes are capped, Host and Origin are rewritten to the loopback address. Hop-by-hop response headers
are removed, Set-Cookie loses its Domain attribute, Location is made path-relative.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import struct
from typing import Any, Awaitable, Callable, Optional

# frame kinds (inside a type-4 plaintext: kind u8, request id u32 big-endian, data)
REQ_HEAD, REQ_BODY, REQ_END, RES_HEAD, RES_BODY, RES_END, ERR, WS_OPEN, WS_DATA, WS_CLOSE, CANCEL = range(11)

MAX_HEAD_BYTES = 64 * 1024
MAX_REQ_BODY = 8 * 1024 * 1024
MAX_RES_BODY = 64 * 1024 * 1024
MAX_PATH = 4096
MAX_CHUNK_LINE = 1024  # a chunk-size line (hex size plus extensions) is never longer than this
MAX_HEADERS = 64
MAX_HEADER_VALUE = 8192
MAX_CONCURRENT = 16
MAX_WS_TUNNELS = 8
CHUNK = 64 * 1024
RES_HEAD_TIMEOUT = 60.0

METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
REQ_HEADER_ALLOW = {
    "accept", "accept-language", "cache-control", "content-type", "cookie", "if-match", "if-modified-since",
    "if-none-match", "if-range", "pragma", "range", "x-xsrftoken", "x-csrftoken", "x-requested-with",
    "sec-websocket-protocol",
}
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "trailers",
    "transfer-encoding", "upgrade", "content-length", "strict-transport-security", "alt-svc", "clear-site-data",
    "public-key-pins",
}
_PATH_RE = re.compile(r"^/[\x21-\x7e]*$")  # printable ASCII, no space, no CR/LF
_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
_DOMAIN_RE = re.compile(r";\s*domain=[^;]*", re.IGNORECASE)

Send = Callable[[bytes], Awaitable[None]]  # sends one type-4 plaintext (already framed) over the encrypted channel


class HttpBridgeError(Exception):
    pass


def pack(kind: int, req_id: int, data: bytes = b"") -> bytes:
    return struct.pack(">BI", kind, req_id) + data


def unpack(payload: bytes) -> tuple[int, int, bytes]:
    if len(payload) < 5:
        raise HttpBridgeError("short http frame")
    kind, req_id = struct.unpack(">BI", payload[:5])
    return kind, req_id, payload[5:]


def clean_request_headers(headers: Any) -> list[tuple[str, str]]:
    """Allowlisted, injection-safe request headers. Raises HttpBridgeError on malformed input."""
    if not isinstance(headers, list) or len(headers) > MAX_HEADERS:
        raise HttpBridgeError("bad headers")
    out: list[tuple[str, str]] = []
    for item in headers:
        if not (isinstance(item, (list, tuple)) and len(item) == 2 and all(isinstance(x, str) for x in item)):
            raise HttpBridgeError("bad header")
        name, value = item[0], item[1]
        if not _NAME_RE.match(name) or len(value) > MAX_HEADER_VALUE or any(c in value for c in "\r\n\x00"):
            raise HttpBridgeError("bad header")
        if name.lower() in REQ_HEADER_ALLOW:
            out.append((name.lower(), value))
    return out


def clean_response_headers(raw: list[tuple[str, str]], port: int) -> list[list[str]]:
    """Drop hop-by-hop headers, strip Set-Cookie Domain, make Location path-relative."""
    out: list[list[str]] = []
    for name, value in raw:
        low = name.lower()
        if low in HOP_BY_HOP:
            continue
        if low == "set-cookie":
            value = _DOMAIN_RE.sub("", value)
        elif low == "location":
            value = re.sub(rf"^https?://(127\.0\.0\.1|localhost|\[::1\]):{port}(?=/|$)", "", value, flags=re.IGNORECASE) or "/"
        out.append([low, value])
    return out


def build_request(method: str, path: str, headers: list[tuple[str, str]], body: bytes, port: int, *, ws_key: Optional[str] = None) -> bytes:
    lines = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Accept-Encoding: identity"]
    if ws_key is None:
        lines.append("Connection: close")
    else:
        lines += ["Connection: Upgrade", "Upgrade: websocket", "Sec-WebSocket-Version: 13", f"Sec-WebSocket-Key: {ws_key}"]
    if ws_key is not None or method not in ("GET", "HEAD"):
        lines.append(f"Origin: http://127.0.0.1:{port}")  # same-origin from the app's point of view (XSRF / WS origin checks)
    lines += [f"{n}: {v}" for n, v in headers]
    if ws_key is None and (body or method in ("POST", "PUT", "PATCH")):
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


class Stream:
    """One byte stream to the app. Real one = docker exec nc; tests inject a fake."""

    def __init__(self, reader: Any, writer: Any, closer: Callable[[], Awaitable[None]]) -> None:
        self.reader, self.writer, self.closer = reader, writer, closer


async def open_docker_stream(argv: list[str]) -> Stream:  # pragma: no cover - needs docker
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def _close() -> None:
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()

    return Stream(proc.stdout, proc.stdin, _close)


async def _read_head(reader: Any) -> tuple[int, list[tuple[str, str]], bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await reader.read(8192)
        if not chunk:
            raise HttpBridgeError("upstream closed before response head")
        buf += chunk
        if len(buf) > MAX_HEAD_BYTES:
            raise HttpBridgeError("response head too large")
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    m = re.match(r"^HTTP/1\.[01] (\d{3})", lines[0])
    if not m:
        raise HttpBridgeError("bad status line")
    hdrs: list[tuple[str, str]] = []
    for ln in lines[1:]:
        k, sep, v = ln.partition(":")
        if sep:
            hdrs.append((k.strip(), v.strip()))
    return int(m.group(1)), hdrs, rest


async def _body_chunks(reader: Any, headers: list[tuple[str, str]], status: int, method: str, rest: bytes):
    """Yield the decoded body: chunked, content-length, or until close. Caps the total at MAX_RES_BODY."""
    h = {k.lower(): v for k, v in headers}
    if method == "HEAD" or status in (204, 304) or 100 <= status < 200:
        return
    total = 0

    async def more() -> bytes:
        return await reader.read(CHUNK)

    if "chunked" in h.get("transfer-encoding", "").lower():
        buf = rest
        while True:
            while b"\r\n" not in buf:
                if len(buf) > MAX_CHUNK_LINE:
                    # The app is untrusted: a "chunk size line" that never ends would otherwise grow this buffer without bound.
                    raise HttpBridgeError("bad chunk size")
                d = await more()
                if not d:
                    raise HttpBridgeError("truncated chunked body")
                buf += d
            line, _, buf = buf.partition(b"\r\n")
            if len(line) > MAX_CHUNK_LINE:
                raise HttpBridgeError("bad chunk size")
            try:
                size = int(line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                raise HttpBridgeError("bad chunk size")
            if size == 0:
                return
            total += size
            if total > MAX_RES_BODY:
                raise HttpBridgeError("response too large")
            while len(buf) < size + 2:
                d = await more()
                if not d:
                    raise HttpBridgeError("truncated chunked body")
                buf += d
            data, buf = buf[:size], buf[size + 2:]
            for i in range(0, len(data), CHUNK):
                yield data[i:i + CHUNK]
        return
    if "content-length" in h:
        try:
            remaining = int(h["content-length"])
        except ValueError:
            raise HttpBridgeError("bad content-length")
        if remaining < 0 or remaining > MAX_RES_BODY:
            raise HttpBridgeError("response too large")
        data = rest[:remaining]
        remaining -= len(data)
        if data:
            yield data
        while remaining > 0:
            d = await more()
            if not d:
                raise HttpBridgeError("truncated body")
            d = d[:remaining]
            remaining -= len(d)
            yield d
        return
    if rest:
        total += len(rest)
        yield rest
    while True:
        d = await more()
        if not d:
            return
        total += len(d)
        if total > MAX_RES_BODY:
            raise HttpBridgeError("response too large")
        yield d


class HttpBridge:
    """Per-handshake bridge state. ``send`` ships a framed payload to the browser; ``opener(argv)`` yields a Stream."""

    def __init__(self, send: Send, argv: list[str], port: int, *, opener: Callable[[list[str]], Awaitable[Stream]] = open_docker_stream,
                 ws_key_factory: Callable[[], str] = lambda: __import__("base64").b64encode(__import__("os").urandom(16)).decode()) -> None:
        self._send, self._argv, self._port, self._open, self._ws_key = send, argv, port, opener, ws_key_factory
        self._reqs: dict[int, dict[str, Any]] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    async def _err(self, rid: int, code: str) -> None:
        with contextlib.suppress(Exception):
            await self._send(pack(ERR, rid, json.dumps({"code": code}).encode()))

    async def on_frame(self, payload: bytes) -> None:
        kind, rid, data = unpack(payload)
        st = self._reqs.get(rid)
        try:
            if kind in (REQ_HEAD, WS_OPEN):
                if st is not None or len(self._reqs) >= MAX_CONCURRENT:
                    return await self._err(rid, "too_many_requests" if st is None else "duplicate_request")
                if kind == WS_OPEN and sum(1 for s in self._reqs.values() if s["ws"]) >= MAX_WS_TUNNELS:
                    return await self._err(rid, "too_many_tunnels")
                meta = json.loads(data.decode())
                method = "GET" if kind == WS_OPEN else str(meta.get("method", ""))
                path = meta.get("path")
                if method not in METHODS or not isinstance(path, str) or len(path) > MAX_PATH or not _PATH_RE.match(path):
                    return await self._err(rid, "bad_request")
                hdrs = clean_request_headers(meta.get("headers", []))
                st = {"method": method, "path": path, "headers": hdrs, "body": bytearray(), "ws": kind == WS_OPEN,
                      "q": asyncio.Queue(), "ended": kind == WS_OPEN}
                self._reqs[rid] = st
                if kind == WS_OPEN:
                    self._tasks[rid] = asyncio.create_task(self._run_ws(rid, st))
            elif kind == REQ_BODY and st is not None and not st["ws"]:
                st["body"] += data
                if len(st["body"]) > MAX_REQ_BODY:
                    self._drop(rid)
                    await self._err(rid, "request_too_large")
            elif kind == REQ_END and st is not None and not st["ws"]:
                st["ended"] = True
                self._tasks[rid] = asyncio.create_task(self._run_http(rid, st))
            elif kind == WS_DATA and st is not None and st["ws"]:
                st["q"].put_nowait(data)
            elif kind in (WS_CLOSE, CANCEL) and st is not None:
                st["q"].put_nowait(None)
                if rid in self._tasks and not st["ws"]:
                    self._tasks[rid].cancel()
                self._reqs.pop(rid, None)
        except (HttpBridgeError, ValueError, UnicodeDecodeError, KeyError):
            self._drop(rid)
            await self._err(rid, "bad_request")

    def _drop(self, rid: int) -> None:
        self._reqs.pop(rid, None)
        t = self._tasks.pop(rid, None)
        if t:
            t.cancel()

    async def close(self) -> None:
        for rid in list(self._reqs):
            self._drop(rid)

    async def _run_http(self, rid: int, st: dict[str, Any]) -> None:
        stream: Optional[Stream] = None
        try:
            stream = await self._open(self._argv)
            stream.writer.write(build_request(st["method"], st["path"], st["headers"], bytes(st["body"]), self._port))
            await stream.writer.drain()
            status, hdrs, rest = await asyncio.wait_for(_read_head(stream.reader), RES_HEAD_TIMEOUT)
            while 100 <= status < 200 and status != 101:  # skip interim responses
                status, hdrs, rest = await asyncio.wait_for(_read_head_from(stream.reader, rest), RES_HEAD_TIMEOUT)
            if any(k.lower() == "content-encoding" and v.lower() != "identity" for k, v in hdrs):
                raise HttpBridgeError("unsupported_content_encoding")
            head = {"status": status, "headers": clean_response_headers(hdrs, self._port)}
            await self._send(pack(RES_HEAD, rid, json.dumps(head).encode()))
            async for chunk in _body_chunks(stream.reader, hdrs, status, st["method"], rest):
                await self._send(pack(RES_BODY, rid, chunk))
            await self._send(pack(RES_END, rid))
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            await self._err(rid, "upstream_timeout")
        except HttpBridgeError as exc:
            await self._err(rid, str(exc)[:60].replace(" ", "_"))
        except Exception:
            await self._err(rid, "upstream_error")
        finally:
            self._reqs.pop(rid, None)
            self._tasks.pop(rid, None)
            if stream is not None:
                with contextlib.suppress(Exception):
                    await stream.closer()

    async def _run_ws(self, rid: int, st: dict[str, Any]) -> None:
        stream: Optional[Stream] = None
        try:
            stream = await self._open(self._argv)
            stream.writer.write(build_request("GET", st["path"], st["headers"], b"", self._port, ws_key=self._ws_key()))
            await stream.writer.drain()
            status, hdrs, rest = await asyncio.wait_for(_read_head(stream.reader), RES_HEAD_TIMEOUT)
            head = {"status": status, "headers": [h for h in clean_response_headers(hdrs, self._port) if h[0] == "sec-websocket-protocol"]}
            await self._send(pack(RES_HEAD, rid, json.dumps(head).encode()))
            if status != 101:
                await self._send(pack(RES_END, rid))
                return
            if rest:
                await self._send(pack(WS_DATA, rid, rest))

            async def up() -> None:  # browser -> app
                while True:
                    d = await st["q"].get()
                    if d is None:
                        return
                    stream.writer.write(d)
                    await stream.writer.drain()

            async def down() -> None:  # app -> browser
                while True:
                    d = await stream.reader.read(CHUNK)
                    if not d:
                        return
                    await self._send(pack(WS_DATA, rid, d))

            tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
            await self._send(pack(WS_CLOSE, rid))
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._err(rid, "upstream_error")
        finally:
            self._reqs.pop(rid, None)
            self._tasks.pop(rid, None)
            if stream is not None:
                with contextlib.suppress(Exception):
                    await stream.closer()


async def _read_head_from(reader: Any, rest: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
    """Continue parsing after an interim (1xx) response: ``rest`` holds bytes already read."""

    class _Pre:
        def __init__(self) -> None:
            self.pending = rest

        async def read(self, n: int) -> bytes:
            if self.pending:
                d, self.pending = self.pending, b""
                return d
            return await reader.read(n)

    return await _read_head(_Pre())
