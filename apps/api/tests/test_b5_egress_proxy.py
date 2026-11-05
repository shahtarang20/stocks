"""B5 egress proxy allow/deny logic (workloads/egress-proxy/proxy.py). WRITTEN, NOT YET RUN.

Pure-logic tests plus the request handler driven through in-memory streams with a fake resolver and a
local TCP echo server standing in for the upstream. No external network.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "workloads", "egress-proxy", "proxy.py")
_spec = importlib.util.spec_from_file_location("egress_proxy", _PATH)
proxy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proxy)

ALLOW = proxy.parse_allowlist("Example.com:443, api.test:8443,")


def test_allowlist_parsing():
    assert ALLOW == {("example.com", 443), ("api.test", 8443)}
    for bad in ("example.com", "example.com:abc", ":443", "example.com:0", "example.com:70000"):
        with pytest.raises(ValueError):
            proxy.parse_allowlist(bad)
    assert proxy.parse_allowlist("") == frozenset()


@pytest.mark.parametrize("ip", [
    "10.0.0.5", "172.16.1.1", "192.168.1.1", "127.0.0.1", "::1", "169.254.169.254", "169.254.1.1",
    "0.0.0.0", "224.0.0.1", "fe80::1", "fd00:ec2::254", "::ffff:10.0.0.1", "100.64.0.1", "fc00::1",
])
def test_forbidden_ips(ip):
    assert proxy.is_forbidden_ip(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_ips_allowed(ip):
    assert not proxy.is_forbidden_ip(ip)


def test_connect_target_parsing():
    assert proxy.parse_connect_target("CONNECT Example.com:443 HTTP/1.1") == ("example.com", 443)
    assert proxy.parse_connect_target("CONNECT [2606:4700::1111]:443 HTTP/1.1") == ("2606:4700::1111", 443)
    for line in ("GET http://example.com/ HTTP/1.1", "CONNECT example.com HTTP/1.1", "CONNECT example.com:x HTTP/1.1", "CONNECT"):
        with pytest.raises(proxy.Denied):
            proxy.parse_connect_target(line)


def test_allow_check_is_exact():
    proxy.check_allowed("example.com", 443, ALLOW)
    for host, port in (("example.com", 80), ("evil.example.com", 443), ("example.com.evil.net", 443), ("api.test", 443)):
        with pytest.raises(proxy.Denied):
            proxy.check_allowed(host, port, ALLOW)


def test_resolved_addresses_dns_rebinding_defence():
    assert proxy.check_resolved(["93.184.216.34"]) == "93.184.216.34"
    with pytest.raises(proxy.Denied):
        proxy.check_resolved(["127.0.0.1"])
    with pytest.raises(proxy.Denied):
        proxy.check_resolved(["93.184.216.34", "169.254.169.254"])  # ANY bad address refuses the name
    with pytest.raises(proxy.Denied):
        proxy.check_resolved([])


class _FakeWriter:
    def __init__(self):
        self.buf, self.closed = b"", False

    def write(self, data): self.buf += data
    async def drain(self): pass
    def close(self): self.closed = True
    def get_extra_info(self, name): return ("127.0.0.1", 1)


async def _drive(request: bytes, resolver, allow=ALLOW):
    reader = asyncio.StreamReader()
    reader.feed_data(request)
    reader.feed_eof()
    writer = _FakeWriter()
    await proxy.make_handler(allow, resolver)(reader, writer)
    return writer.buf


@pytest.mark.asyncio
async def test_denials_answer_403_and_log_denied(caplog):
    async def public(host, port): return ["93.184.216.34"]
    async def internal(host, port): return ["10.1.2.3"]
    caplog.set_level(logging.INFO, logger="egress-proxy")
    out = await _drive(b"CONNECT other.com:443 HTTP/1.1\r\nHost: other.com\r\n\r\n", public)
    assert out.startswith(b"HTTP/1.1 403")
    out = await _drive(b"GET http://example.com/ HTTP/1.1\r\n\r\n", public)
    assert out.startswith(b"HTTP/1.1 403")
    out = await _drive(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n", internal)  # allowlisted name, internal address
    assert out.startswith(b"HTTP/1.1 403")
    assert sum("DENIED" in r.getMessage() for r in caplog.records) == 3
    assert any("resolves_to_forbidden_address" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_allowed_connects_to_resolved_ip_not_name():
    # An upstream on loopback stands in for the real server: the policy layer is bypassed only for this
    # test by patching is_forbidden_ip, so we can prove the connection goes to the RESOLVED ip.
    async def echo(r, w):
        w.write(b"hello")
        await w.drain()
        w.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    allow = proxy.parse_allowlist(f"upstream.invalid:{port}")
    seen = []

    async def resolver(host, p):
        seen.append(host)
        return ["127.0.0.1"]

    original = proxy.is_forbidden_ip
    proxy.is_forbidden_ip = lambda ip: False
    try:
        pr, pw = asyncio.StreamReader(), _FakeWriter()
        pr.feed_data(f"CONNECT upstream.invalid:{port} HTTP/1.1\r\n\r\n".encode())
        task = asyncio.create_task(proxy.make_handler(allow, resolver)(pr, pw))
        await asyncio.sleep(0.3)
        pr.feed_eof()
        await asyncio.wait_for(task, 5)
    finally:
        proxy.is_forbidden_ip = original
        server.close()
    assert seen == ["upstream.invalid"]
    assert pw.buf.startswith(b"HTTP/1.1 200") and pw.buf.endswith(b"hello")
