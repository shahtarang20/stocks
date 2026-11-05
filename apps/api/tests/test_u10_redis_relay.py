"""U10: RedisRelayBackend with an in-memory fake Redis shared by two backend instances (two "workers").

The fake follows the redis-py asyncio surface the backend uses: pubsub().subscribe/listen/aclose, publish (returns
the subscriber count), set/get/exists/delete. Written, not run.
"""

from __future__ import annotations

import asyncio

from app.services import session_relay as relay
from app.services.session_relay import RedisRelayBackend


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, bytes] = {}
        self.subs: dict[str, list[asyncio.Queue]] = {}

    def pubsub(self):
        return FakePubSub(self)

    async def publish(self, channel: str, payload: bytes) -> int:
        queues = list(self.subs.get(channel, []))
        for q in queues:
            q.put_nowait({"type": "message", "data": payload})
        return len(queues)

    async def set(self, key, value, ex=None):
        self.kv[key] = value

    async def get(self, key):
        return self.kv.get(key)

    async def exists(self, key):
        return 1 if key in self.kv else 0

    async def delete(self, *keys):
        return sum(1 for k in keys if self.kv.pop(k, None) is not None)


class FakePubSub:
    def __init__(self, server: FakeRedis) -> None:
        self.server = server
        self.queue: asyncio.Queue = asyncio.Queue()
        self.channels: list[str] = []

    async def subscribe(self, channel: str) -> None:
        self.channels.append(channel)
        self.server.subs.setdefault(channel, []).append(self.queue)

    async def listen(self):
        while True:
            yield await self.queue.get()

    async def aclose(self) -> None:
        for ch in self.channels:
            if self.queue in self.server.subs.get(ch, []):
                self.server.subs[ch].remove(self.queue)


class Conn:
    def __init__(self) -> None:
        self.sent: list = []
        self.closed: tuple | None = None

    async def send(self, data):
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = (code, reason)


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_forward_between_two_workers_binary_and_text():
    server = FakeRedis()
    w1, w2 = RedisRelayBackend("redis://fake", client=server), RedisRelayBackend("redis://fake", client=server)
    host, browser = Conn(), Conn()
    await w1.attach("s", relay.HOST, host)
    await w2.attach("s", relay.BROWSER, browser)
    assert await w1.forward("s", relay.HOST, b"\x01\x02") is True  # host worker -> browser worker
    assert await w2.forward("s", relay.BROWSER, '{"type":"hello"}') is True
    await _settle()
    assert browser.sent == [b"\x01\x02"] and host.sent == ['{"type":"hello"}']


async def test_frame_for_absent_peer_is_dropped_not_buffered():
    server = FakeRedis()
    w = RedisRelayBackend("redis://fake", client=server)
    await w.attach("s", relay.HOST, Conn())
    assert await w.forward("s", relay.HOST, b"x") is False  # nobody subscribed on the browser channel
    assert await w.forward("s", relay.HOST, b"x" * (relay.MAX_FRAME_BYTES + 1)) is False


async def test_presence_and_activity_are_shared_between_workers():
    server = FakeRedis()
    clock = iter(range(100, 200))
    w1 = RedisRelayBackend("redis://fake", client=server, clock=lambda: float(next(clock)))
    w2 = RedisRelayBackend("redis://fake", client=server)
    await w1.attach("s", relay.HOST, Conn())
    await w2.attach("s", relay.BROWSER, Conn())
    await _settle()
    assert await w2.peer_connected_async("s", relay.HOST) is True  # seen via the shared presence key
    assert await w2.last_activity_async("s") is None  # text frames and silence are not activity
    await w1.forward("s", relay.HOST, '{"type":"hello"}')
    assert await w2.last_activity_async("s") is None
    await w1.forward("s", relay.HOST, b"\x00")
    assert await w2.last_activity_async("s") == 100.0  # binary frame counted, visible from the other worker


async def test_detach_clears_presence():
    server = FakeRedis()
    w = RedisRelayBackend("redis://fake", client=server)
    c = Conn()
    await w.attach("s", relay.HOST, c)
    await w.detach("s", relay.HOST, c)
    assert await w.peer_connected_async("s", relay.HOST) is False


async def test_new_connection_on_another_worker_replaces_the_old_one():
    server = FakeRedis()
    w1, w2 = RedisRelayBackend("redis://fake", client=server), RedisRelayBackend("redis://fake", client=server)
    old, new = Conn(), Conn()
    await w1.attach("s", relay.HOST, old)
    await w2.attach("s", relay.HOST, new)
    await _settle()
    assert old.closed == (4000, "replaced")
    assert new.closed is None  # a worker ignores the replace notice carrying its own token


async def test_close_session_closes_both_sides_everywhere():
    server = FakeRedis()
    w1, w2 = RedisRelayBackend("redis://fake", client=server), RedisRelayBackend("redis://fake", client=server)
    host, browser = Conn(), Conn()
    await w1.attach("s", relay.HOST, host)
    await w2.attach("s", relay.BROWSER, browser)
    await w1.close_session("s", "idle_timeout")
    await _settle()
    assert host.closed == (4001, "idle_timeout") and browser.closed == (4001, "idle_timeout")
    assert await w2.last_activity_async("s") is None


def test_get_relay_selects_backend_from_setting(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(relay, "_backend", None)
    monkeypatch.setattr(settings, "SESSIONS_RELAY_BACKEND", "redis", raising=False)
    assert isinstance(relay.get_relay(), RedisRelayBackend)
    monkeypatch.setattr(relay, "_backend", None)
    monkeypatch.setattr(settings, "SESSIONS_RELAY_BACKEND", "memory", raising=False)
    assert isinstance(relay.get_relay(), relay.MemoryRelayBackend)
