"""Rate limiting with a Redis fixed-window backend and an in-process fallback.

The limiter feature-detects Redis at first use. When Redis is unreachable (e.g.
tests running offline), it transparently falls back to an in-process limiter so
the behaviour and the 429s are still exercised.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

from app.core.config import settings


class InProcessRateLimiter:
    """Simple thread-safe fixed-window counter keyed by an arbitrary string."""

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


class RateLimiter:
    """Facade that prefers Redis and falls back to the in-process limiter."""

    def __init__(self) -> None:
        self._fallback = InProcessRateLimiter()
        self._redis = None
        self._redis_checked = False

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
            # fallback multiplies the effective limit across workers — surface it
            # loudly so operators provision Redis (required in production).
            if settings.ENVIRONMENT.lower() not in {"development", "dev", "local"}:
                logging.getLogger("computecoin.ratelimit").warning(
                    "Redis unavailable at %s — rate limiting degraded to per-process "
                    "fallback. Provision Redis for correct limits in production.",
                    settings.REDIS_URL,
                )
        return self._redis

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        client = self._get_redis()
        if client is not None:  # pragma: no cover - requires live Redis
            try:
                redis_key = f"rl:{key}"
                current = client.incr(redis_key)
                if current == 1:
                    client.expire(redis_key, window_seconds)
                return int(current) <= limit
            except Exception:
                # Redis died mid-flight: degrade to in-process.
                self._redis = None
        return self._fallback.allow(key, limit, window_seconds)

    def reset(self, key: str | None = None) -> None:
        self._fallback.reset(key)
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
