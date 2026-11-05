"""U10 sessions (written, NOT executed): validation, deadlines, relay forwarding."""

import asyncio
import datetime as dt
import types

import pytest

from app.services import session_relay as relay
from app.services import sessions as svc

K = "a" * 64


def _v(**kw):
    base = dict(isolation_tier="container", kit_image="img@sha256:" + "b" * 64, image_digest=None, gpu_count=1,
                max_duration_seconds=3600, idle_timeout_seconds=600, client_pubkey=K, network_policy=None,
                mode="pty", tcp_port=None)
    base.update(kw)
    return svc.validate_request(**base)


@pytest.mark.parametrize("tier", ["sealed", "sealed_pro", "confidential"])
def test_private_tiers_refused(tier):
    with pytest.raises(svc.SessionError) as e:
        _v(isolation_tier=tier)
    assert e.value.code == "sessions_not_available_for_private_tiers"


def test_validation_rules():
    assert _v() is None
    with pytest.raises(svc.SessionError):
        _v(client_pubkey="zz")
    with pytest.raises(svc.SessionError):
        _v(idle_timeout_seconds=10)
    with pytest.raises(svc.SessionError):
        _v(kit_image=None, image_digest=None)
    with pytest.raises(svc.SessionError):  # custom images are off by default
        _v(kit_image=None, image_digest="x@sha256:" + "c" * 64)


def _row(**kw):
    now = dt.datetime.now(dt.timezone.utc)
    base = dict(status="active", created_at=now, started_at=now, last_activity_at=now, last_host_heartbeat_at=now,
                max_duration_seconds=3600, idle_timeout_seconds=600)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_deadlines():
    now = dt.datetime.now(dt.timezone.utc)
    assert svc.deadline_reason(_row(), now) is None
    old = now - dt.timedelta(seconds=700)
    assert svc.deadline_reason(_row(last_activity_at=old), now) == "idle_timeout"
    assert svc.deadline_reason(_row(started_at=now - dt.timedelta(hours=2), last_activity_at=now), now) == "max_duration"
    assert svc.deadline_reason(_row(last_host_heartbeat_at=now - dt.timedelta(seconds=200)), now) == "host_lost"
    assert svc.deadline_reason(_row(status="pending", created_at=now - dt.timedelta(seconds=700)), now) == "unclaimed"


class Conn:
    def __init__(self):
        self.got, self.closed = [], None

    async def send(self, data):
        self.got.append(data)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


def test_relay_forwards_opaque_and_counts_binary_only():
    async def go():
        r = relay.MemoryRelayBackend(clock=lambda: 5.0)
        h, b = Conn(), Conn()
        await r.attach("s", relay.HOST, h)
        await r.attach("s", relay.BROWSER, b)
        assert await r.forward("s", relay.BROWSER, '{"type":"hello"}') and r.last_activity("s") is None
        assert await r.forward("s", relay.BROWSER, b"\x01\x02") and r.last_activity("s") == 5.0
        assert h.got == ['{"type":"hello"}', b"\x01\x02"]
        assert not await r.forward("s", relay.HOST, b"x" * (relay.MAX_FRAME_BYTES + 1))
        await r.close_session("s", "idle_timeout")
        assert h.closed and b.closed and r.last_activity("s") is None

    asyncio.run(go())


def test_relay_replaces_old_connection():
    async def go():
        r = relay.MemoryRelayBackend()
        a, b = Conn(), Conn()
        await r.attach("s", relay.HOST, a)
        await r.attach("s", relay.HOST, b)
        assert a.closed and not b.closed

    asyncio.run(go())
