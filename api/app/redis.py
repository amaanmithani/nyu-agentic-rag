"""Redis wrapper with a circuit breaker.

Redis is an optimisation (cache, session state, job status), never a hard
dependency. Every call uses short timeouts; after a failure the breaker opens
for BREAKER_COOLDOWN_S and calls return None immediately, so a Redis outage
costs at most one slow call per cooldown instead of one per request.
Callers treat None as "cache miss / not available" and fall back to Postgres.
"""

import logging
import time
from typing import Any

import redis

from app.config import get_settings

log = logging.getLogger(__name__)

BREAKER_COOLDOWN_S = 15.0
UNAVAILABLE = object()  # sentinel: the call did not reach Redis


class SafeRedis:
    def __init__(self, url: str):
        self._client = redis.Redis.from_url(
            url, socket_connect_timeout=0.5, socket_timeout=0.5, decode_responses=True
        )
        self._open_until = 0.0

    @property
    def available(self) -> bool:
        return time.monotonic() >= self._open_until

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if not self.available:
            return UNAVAILABLE
        try:
            return getattr(self._client, method)(*args, **kwargs)
        except redis.RedisError as e:
            log.warning("redis %s failed, breaker open %ss: %s", method, BREAKER_COOLDOWN_S, e)
            self._open_until = time.monotonic() + BREAKER_COOLDOWN_S
            return UNAVAILABLE

    def _or_none(self, value: Any) -> Any:
        return None if value is UNAVAILABLE else value

    def ping(self) -> bool:
        # Health checks bypass the breaker so recovery is noticed immediately.
        try:
            ok = bool(self._client.ping())
            self._open_until = 0.0
            return ok
        except redis.RedisError:
            return False

    def get(self, key: str) -> str | None:
        return self._or_none(self._call("get", key))

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool | None:
        """True = written, False = not written (nx and key exists), None = Redis unavailable.

        redis-py itself returns None when an NX set finds the key, which would be
        indistinguishable from "unavailable"; normalize it to False here.
        """
        result = self._call("set", key, value, ex=ex, nx=nx)
        return None if result is UNAVAILABLE else bool(result)

    def delete(self, key: str) -> int | None:
        return self._or_none(self._call("delete", key))

    def expire(self, key: str, seconds: int) -> bool | None:
        return self._or_none(self._call("expire", key, seconds))

    def incr(self, key: str) -> int | None:
        return self._or_none(self._call("incr", key))


_redis: SafeRedis | None = None


def get_redis() -> SafeRedis:
    global _redis
    if _redis is None:
        _redis = SafeRedis(get_settings().redis_url)
    return _redis
