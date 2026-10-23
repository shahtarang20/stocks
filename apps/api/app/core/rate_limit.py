"""Rate limiting: Redis sliding-window backend with an in-process fallback.

Design decisions worth knowing before you change this file
----------------------------------------------------------

**Sliding window, not fixed window.** A fixed window lets a caller spend its
whole budget in the last millisecond of window *N* and the whole budget again in
the first millisecond of window *N+1* — a 2x burst straddling the edge. Both
backends now keep per-hit timestamps (an in-memory list, or a Redis sorted set
scored by time) and evict anything older than ``window_seconds`` on each call,
so the limit holds over *any* window of that length.

**Degrade strict, don't fail closed.** ``settings.RATE_LIMIT_REQUIRE_REDIS``
means "Redis is mandatory in this deployment". Without a shared counter the
per-process fallback multiplies the effective limit by the worker count, so we
must not pretend nothing happened. But failing the whole API closed on a Redis
blip turns a cache outage into a full outage, which is a worse incident than a
briefly sloppy limit. So when Redis is required and unreachable we keep serving
and clamp the in-process limit to ``ceil(limit / STRICT_DEGRADE_DIVISOR)``
(floor ``STRICT_DEGRADE_MIN``) and log loudly. Operators get 429s and an alert,
not a dead API.

(The ``RATE_LIMIT_REQUIRE_REDIS`` comment in config.py predates this decision
and describes the fail-closed behaviour that was rejected for the reason above.)

Dev and test keep the plain in-process limiter: ``settings.is_test`` never
contacts Redis, so the suite runs offline and still exercises real 429s.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict

from app.core.config import settings

logger = logging.getLogger("computecoin.ratelimit")

# How hard we clamp the limit when a Redis-mandatory deployment loses Redis.
STRICT_DEGRADE_DIVISOR = 10
STRICT_DEGRADE_MIN = 1
# Loud, but not so loud it becomes the outage: one warning per key-space per
# this many seconds while degraded.
_DEGRADE_LOG_INTERVAL_SECONDS = 30.0

_DEV_LIKE_ENVIRONMENTS = {"development", "dev", "local", "test", "testing"}


class InProcessRateLimiter:
    """Thread-safe sliding-window counter keyed by an arbitrary string.

    Holds one timestamp per accepted hit inside the window. ``limit`` is small
    (tens to low thousands) for every caller in this codebase, so the list stays
    bounded; a token bucket would trade this exactness for memory we do not need
    to save.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> list[timestamps]
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            hits = [t for t in self._hits[key] if t > cutoff]
            if len(hits) >= limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


def _is_dev_like() -> bool:
    return settings.ENVIRONMENT.lower() in _DEV_LIKE_ENVIRONMENTS


class RateLimiter:
    """Facade that prefers Redis and falls back to the in-process limiter."""

    def __init__(self) -> None:
        self._fallback = InProcessRateLimiter()
        self._redis = None
        self._redis_checked = False
        self._degraded = False
        self._last_degrade_log = 0.0

    # -- backend selection --------------------------------------------------
    def _get_redis(self):
        if self._redis_checked:
            return self._redis
        self._redis_checked = True
        # In tests we never want to touch a live Redis.
        if settings.is_test:
            self._redis = None
            return None
        try:  # pragma: no cover - exercised only with a live Redis
            import redis  # local import so tests don't require a running server

            client = redis.Redis.from_url(
                settings.REDIS_URL, socket_connect_timeout=0.25, socket_timeout=0.25
            )
            client.ping()
            self._redis = client
        except Exception:
            self._redis = None
            # In a non-dev deployment, running without Redis means the per-process
            # fallback multiplies the effective limit across workers.
            if not _is_dev_like():
                logger.warning(
                    "Redis unavailable at %s — rate limiting degraded to per-process "
                    "fallback. Provision Redis for correct limits in production.",
                    settings.REDIS_URL,
                )
        return self._redis

    # -- strict degradation -------------------------------------------------
    def redis_mandatory(self) -> bool:
        """True when this deployment must have a shared limiter."""
        return bool(settings.RATE_LIMIT_REQUIRE_REDIS) and not _is_dev_like()

    @property
    def degraded(self) -> bool:
        """True while a Redis-mandatory deployment is running without Redis."""
        return self._degraded

    def effective_limit(self, limit: int) -> int:
        """The limit actually enforced by the in-process fallback.

        Normally the caller's limit. While a Redis-mandatory deployment has no
        Redis, a strict clamp — every worker independently allows this many, so
        the cluster-wide total stays near the intended budget instead of being
        multiplied by the worker count.
        """
        if not self.redis_mandatory():
            return limit
        return max(STRICT_DEGRADE_MIN, math.ceil(limit / STRICT_DEGRADE_DIVISOR))

    def _note_degraded(self, limit: int, effective: int) -> None:
        self._degraded = True
        now = time.monotonic()
        if now - self._last_degrade_log >= _DEGRADE_LOG_INTERVAL_SECONDS:
            self._last_degrade_log = now
            logger.error(
                "RATE LIMIT DEGRADED: RATE_LIMIT_REQUIRE_REDIS is set but Redis at %s "
                "is unreachable. Serving with a strict per-process limit (%d -> %d per "
                "window) until Redis returns.",
                settings.REDIS_URL,
                limit,
                effective,
            )

    # -- public API ---------------------------------------------------------
    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        client = self._get_redis()
        if client is not None:  # pragma: no cover - requires live Redis
            try:
                allowed = self._allow_redis(client, key, limit, window_seconds)
                self._degraded = False
                return allowed
            except Exception:
                # Redis died mid-flight: degrade to in-process rather than 500.
                self._redis = None
                client = None

        effective = self.effective_limit(limit)
        if effective != limit:
            self._note_degraded(limit, effective)
        else:
            self._degraded = False
        return self._fallback.allow(key, effective, window_seconds)

    def _allow_redis(  # pragma: no cover - requires live Redis
        self, client, key: str, limit: int, window_seconds: int
    ) -> bool:
        """Sliding window over a Redis sorted set.

        One pipeline per call: evict expired members, count what is left, add
        this hit, refresh the TTL. The count is taken *before* the insert, so
        the Nth hit in a window is the last one allowed. A rejected hit is
        removed again so a blocked caller cannot extend their own penalty
        window indefinitely.
        """
        now = time.time()
        redis_key = f"rl:{key}"
        member = f"{now!r}:{id(self)}"
        pipe = client.pipeline()
        pipe.zremrangebyscore(redis_key, 0, now - window_seconds)
        pipe.zcard(redis_key)
        pipe.zadd(redis_key, {member: now})
        pipe.expire(redis_key, window_seconds + 1)
        _, current, _, _ = pipe.execute()
        if int(current) >= limit:
            client.zrem(redis_key, member)
            return False
        return True

    def reset(self, key: str | None = None) -> None:
        self._fallback.reset(key)
        self._degraded = False
        self._last_degrade_log = 0.0
        client = self._redis
        if client is not None:  # pragma: no cover
            try:
                if key is None:
                    for k in client.scan_iter("rl:*"):
                        client.delete(k)
                else:
                    client.delete(f"rl:{key}")
            except Exception:
                pass


rate_limiter = RateLimiter()
