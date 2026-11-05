"""Network benchmark between hosts of one approved datacenter group (U13). Written, never run.

Responder: a tiny TCP service on the private cluster address. A probe sends ``nonce``; the peer answers with
HMAC(peer machine signing key, nonce | prober id) (the "signed benchmark endpoint") and then echoes/sinks bytes for the
bandwidth test. The API verifies that proof with the peer's key, so a host cannot invent results for a peer that never
answered. Prober: measures round-trip latency and one-direction bandwidth to each planned peer and returns the report
body for POST /v1/agents/benchmark/report. The responder only answers when the nonce has not expired.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import socketserver
import threading
import time
from typing import Any

PORT = 29600
PING_ROUNDS = 20
BW_BYTES = 256 * 1024 * 1024
CHUNK = 1 << 20
TIMEOUT_S = 10.0


def proof(signing_key: str, nonce: str, prober_id: str) -> str:
    return hmac.new(signing_key.encode(), f"{nonce}\n{prober_id}".encode(), hashlib.sha256).hexdigest()


def _readline(sock: socket.socket) -> bytes:
    buf = b""
    while not buf.endswith(b"\n") and len(buf) < 4096:
        c = sock.recv(1)
        if not c:
            break
        buf += c
    return buf


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:  # pragma: no cover - needs sockets
        self.request.settimeout(TIMEOUT_S)
        try:
            hello = json.loads(_readline(self.request))
            nonce, prober = str(hello["nonce"]), str(hello["prober_id"])
            exp = int(nonce.split(".", 1)[0])
            if exp < time.time():
                return
            self.request.sendall((json.dumps({"proof": proof(self.server.signing_key, nonce, prober)}) + "\n").encode())
            for _ in range(PING_ROUNDS):
                b = self.request.recv(1)
                if not b:
                    return
                self.request.sendall(b)
            remaining = BW_BYTES
            while remaining > 0:
                data = self.request.recv(min(CHUNK, remaining))
                if not data:
                    return
                remaining -= len(data)
            self.request.sendall(b"k")
        except (OSError, ValueError, KeyError):
            return


def start_responder(bind_addr: str, signing_key: str, port: int = PORT) -> socketserver.ThreadingTCPServer:  # pragma: no cover
    srv = socketserver.ThreadingTCPServer((bind_addr, port), _Handler)
    srv.signing_key = signing_key  # type: ignore[attr-defined]
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def probe_peer(machine_id: str, peer: dict[str, Any], port: int = PORT) -> dict[str, Any]:  # pragma: no cover
    """Measure one peer. Raises OSError on failure; the caller skips that peer (no result = no multi-node)."""
    with socket.create_connection((peer["addr"], port), timeout=TIMEOUT_S) as s:
        s.settimeout(TIMEOUT_S)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall((json.dumps({"nonce": peer["nonce"], "prober_id": machine_id}) + "\n").encode())
        reply = json.loads(_readline(s))
        rtts = []
        for _ in range(PING_ROUNDS):
            t0 = time.perf_counter()
            s.sendall(b"p")
            s.recv(1)
            rtts.append((time.perf_counter() - t0) * 1000.0)
        rtts.sort()
        blob = b"\0" * CHUNK
        t0 = time.perf_counter()
        for _ in range(BW_BYTES // CHUNK):
            s.sendall(blob)
        s.recv(1)
        secs = max(time.perf_counter() - t0, 1e-6)
    return {"peer_id": peer["peer_id"], "nonce": peer["nonce"], "peer_proof": reply["proof"],
            "latency_ms": round(rtts[len(rtts) // 2], 4), "bandwidth_mbps": round(BW_BYTES * 8 / secs / 1e6, 2),
            "samples": PING_ROUNDS}


def build_report(own_addr: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"own_addr": own_addr, "results": results}


# --------------------------------------------------------------------------- #
# Asyncio responder bound to the cluster address only, and the periodic prober loop (agent `start`, netbench_enabled).
# Written, never run. The responder speaks the same wire protocol as _Handler above.
# --------------------------------------------------------------------------- #
import asyncio  # noqa: E402
import ipaddress  # noqa: E402

PROBE_INTERVAL_S = 600.0


def _private_literal(addr: str) -> str:
    """The cluster address must be a literal private IP: never a hostname, wildcard, loopback or public address."""
    ip = ipaddress.ip_address(str(addr))
    if not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_multicast:
        raise ValueError("cluster_addr must be a literal private address")
    return str(ip)


async def _serve_one(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, signing_key: str) -> None:
    try:
        async def _io() -> None:
            line = await reader.readline()
            if not line or len(line) > 4096:
                return
            hello = json.loads(line)
            nonce, prober = str(hello["nonce"]), str(hello["prober_id"])
            if int(nonce.split(".", 1)[0]) < time.time():
                return
            writer.write((json.dumps({"proof": proof(signing_key, nonce, prober)}) + "\n").encode())
            await writer.drain()
            for _ in range(PING_ROUNDS):
                writer.write(await reader.readexactly(1))
                await writer.drain()
            remaining = BW_BYTES
            while remaining > 0:
                data = await reader.read(min(CHUNK, remaining))
                if not data:
                    return
                remaining -= len(data)
            writer.write(b"k")
            await writer.drain()

        await asyncio.wait_for(_io(), timeout=TIMEOUT_S * 6)
    except (OSError, ValueError, KeyError, asyncio.IncompleteReadError, asyncio.TimeoutError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def serve_responder(bind_addr: str, signing_key: str, port: int = PORT) -> asyncio.AbstractServer:  # pragma: no cover
    """Start the responder on ``bind_addr`` ONLY (a literal private address; ValueError otherwise)."""
    host = _private_literal(bind_addr)
    return await asyncio.start_server(lambda r, w: _serve_one(r, w, signing_key), host=host, port=port)


def _signed_get(base_url: str, machine_id: str, machine_secret: str, path: str) -> Any:  # pragma: no cover
    import httpx

    from .heartbeat import sign_contract

    ts = int(time.time())
    headers = {"X-ComputeCoin-Machine": machine_id, "X-ComputeCoin-Timestamp": str(ts),
               "X-ComputeCoin-Signature": sign_contract(machine_id, ts, b"", machine_secret)}
    return httpx.get(base_url.rstrip("/") + path, headers=headers, timeout=30.0)


def _signed_post_json(base_url: str, machine_id: str, machine_secret: str, path: str, body: dict) -> Any:  # pragma: no cover
    import httpx

    from .heartbeat import sign_contract

    raw = json.dumps(body, separators=(",", ":")).encode()
    ts = int(time.time())
    headers = {"Content-Type": "application/json", "X-ComputeCoin-Machine": machine_id,
               "X-ComputeCoin-Timestamp": str(ts), "X-ComputeCoin-Signature": sign_contract(machine_id, ts, raw, machine_secret)}
    return httpx.post(base_url.rstrip("/") + path, content=raw, headers=headers, timeout=30.0)


def probe_once(base_url: str, machine_id: str, machine_secret: str, cluster_addr: str) -> int:  # pragma: no cover
    """One plan -> probe every peer -> report cycle. Returns the number of peers measured. A peer that fails is skipped
    (no result = never used for multi-node)."""
    resp = _signed_get(base_url, machine_id, machine_secret, "/v1/agents/benchmark/plan")
    if resp.status_code != 200:
        return 0
    results = []
    for peer in resp.json().get("peers", []):
        try:
            results.append(probe_peer(machine_id, peer))
        except (OSError, ValueError, KeyError):
            continue
    _signed_post_json(base_url, machine_id, machine_secret, "/v1/agents/benchmark/report", build_report(cluster_addr, results))
    return len(results)


def start_netbench(base_url: str, machine_id: str, machine_secret: str, cluster_addr: str,
                   interval: float = PROBE_INTERVAL_S, emit: Any = None) -> threading.Thread:  # pragma: no cover
    """Daemon thread: asyncio responder on ``cluster_addr`` only, plus the periodic prober. Raises ValueError for a
    bad address before anything starts. The responder's key is the machine's signing key (what the API verifies)."""
    from .heartbeat import signing_key_from_secret

    addr = _private_literal(cluster_addr)
    key = signing_key_from_secret(machine_secret)

    async def _main() -> None:
        try:
            await serve_responder(addr, key)
        except OSError as exc:
            if emit:
                emit(f"[netbench] responder not started: {exc}")
            return
        loop = asyncio.get_running_loop()
        while True:
            try:
                n = await loop.run_in_executor(None, probe_once, base_url, machine_id, machine_secret, addr)
                if emit:
                    emit(f"[netbench] probed {n} peer(s)")
            except Exception as exc:  # never stop the loop
                if emit:
                    emit(f"[netbench] probe failed: {type(exc).__name__}")
            await asyncio.sleep(interval)

    t = threading.Thread(target=lambda: asyncio.run(_main()), name="netbench", daemon=True)
    t.start()
    return t
