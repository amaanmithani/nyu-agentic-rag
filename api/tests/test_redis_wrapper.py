"""SafeRedis against a fake *redis-py client*, mimicking redis-py's real return values."""

import redis

from app.redis import SafeRedis


class FakeClient:
    """redis-py semantics: SET NX on an existing key returns None (not False)."""

    def __init__(self):
        self.data, self.down = {}, False

    def _check(self):
        if self.down:
            raise redis.ConnectionError("connection refused")

    def set(self, k, v, ex=None, nx=False):
        self._check()
        if nx and k in self.data:
            return None
        self.data[k] = v
        return True

    def get(self, k):
        self._check()
        return self.data.get(k)


def make() -> tuple[SafeRedis, FakeClient]:
    r = SafeRedis("redis://unused:6379/0")
    r._client = FakeClient()
    return r, r._client


def test_nx_on_existing_key_is_false_not_unavailable():
    r, _ = make()
    assert r.set("lock", "1", nx=True) is True
    assert r.set("lock", "1", nx=True) is False  # held, not "Redis down"


def test_unavailable_returns_none_and_opens_breaker():
    r, client = make()
    client.down = True
    assert r.set("k", "v") is None
    assert r.available is False
    client.down = False
    assert r.get("k") is None  # breaker still open: no call is made during the cooldown
