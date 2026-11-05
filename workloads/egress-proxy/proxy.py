"""ComputeCoin egress proxy (B5): a CONNECT-only allowlist proxy, standard library only.

Environment:
  COMPUTECOIN_EGRESS_ALLOW   comma-separated ``host:port`` entries; only these CONNECT targets are allowed
  COMPUTECOIN_PROXY_PORT    listen port (default 3128)
  COMPUTECOIN_PROXY_BIND    listen address (default 0.0.0.0, inside the workload's private network)

Rules: only the CONNECT method; the target host:port must be in the allowlist (exact match, host
lower-cased, no wildcards); the proxy resolves the name ITSELF, refuses if ANY resolved address is
private / loopback / link-local / multicast / reserved / unspecified / cloud-metadata (DNS-rebinding
defence: a name that resolves to an internal address is refused even if the name is allowlisted), then
connects to the resolved IP, never to the name again. Every refusal is logged with the word DENIED.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import sys

log = logging.getLogger("egress-proxy")
MAX_HEADER_BYTES = 8192
IDLE_SECONDS = 300
CONNECT_TIMEOUT = 10
METADATA_IPS = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}


class Denied(Exception):
    """A request the policy refuses; the message is a stable reason code."""


def parse_allowlist(value: str) -> frozenset[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for item in (value or "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        host, sep, port = item.rpartition(":")
        if not sep or not host or not port.isdigit() or not (0 < int(port) < 65536):
            raise ValueError(f"bad allowlist entry: {item!r} (want host:port)")
        out.add((host.strip("[]"), int(port)))
    return frozenset(out)


def is_forbidden_ip(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    addr = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped  # ::ffff:10.0.0.1 is still 10.0.0.1
    return (
        addr in METADATA_IPS or addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
        or addr.is_reserved or addr.is_unspecified or not addr.is_global
    )


def parse_connect_target(request_line: str) -> tuple[str, int]:
    parts = request_line.split()
    if len(parts) != 3 or parts[0].upper() != "CONNECT":
        raise Denied("method_not_connect")
    host, sep, port = parts[1].rpartition(":")
    if not sep or not host or not port.isdigit() or not (0 < int(port) < 65536):
        raise Denied("bad_target")
    return host.strip("[]").lower(), int(port)


def check_allowed(host: str, port: int, allow: frozenset[tuple[str, int]]) -> None:
    if (host, port) not in allow:
        raise Denied("target_not_allowlisted")


def check_resolved(addresses: list[str]) -> str:
    """Return the IP to connect to, or raise Denied when nothing resolved or ANY address is forbidden."""
    if not addresses:
        raise Denied("no_address")
    for a in addresses:
        if is_forbidden_ip(a):
            raise Denied("resolves_to_forbidden_address")
    return addresses[0]


async def resolve(host: str, port: int) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise Denied("resolution_failed") from exc
    seen: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.append(ip)
    return seen


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), IDLE_SECONDS)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def make_handler(allow: frozenset[tuple[str, int]], resolver=resolve):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        target = "?"
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
                raise Denied("bad_request")
            if len(head) > MAX_HEADER_BYTES:
                raise Denied("header_too_large")
            host, port = parse_connect_target(head.split(b"\r\n", 1)[0].decode("latin-1"))
            target = f"{host}:{port}"
            check_allowed(host, port, allow)
            ip = check_resolved(await resolver(host, port))
            try:
                up_r, up_w = await asyncio.wait_for(asyncio.open_connection(ip, port), CONNECT_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as exc:
                log.warning("FAILED connect %s via %s: %s", target, ip, type(exc).__name__)
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            log.info("ALLOWED %s -> %s (peer %s)", target, ip, peer)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
        except Denied as exc:
            log.warning("DENIED %s reason=%s peer=%s", target, exc, peer)
            try:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    return handle


async def serve() -> None:
    allow = parse_allowlist(os.environ.get("COMPUTECOIN_EGRESS_ALLOW", ""))
    port = int(os.environ.get("COMPUTECOIN_PROXY_PORT", "3128"))
    bind = os.environ.get("COMPUTECOIN_PROXY_BIND", "0.0.0.0")
    if not allow:
        log.warning("empty allowlist: every request will be DENIED")
    server = await asyncio.start_server(make_handler(allow), bind, port, limit=MAX_HEADER_BYTES)
    log.info("egress proxy on %s:%s allowing %s", bind, port, sorted(f"{h}:{p}" for h, p in allow))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(serve())
