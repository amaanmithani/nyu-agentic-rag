"""Session store: Redis fast path, Postgres fallback, locking, clarification state."""

import json
from types import SimpleNamespace

import pytest

from app import sessions


class FakeRedis:
    def __init__(self, up: bool = True):
        self.up, self.data, self.ttl = up, {}, {}

    def get(self, k):
        return self.data.get(k) if self.up else None

    def set(self, k, v, ex=None, nx=False):
        if not self.up:
            return None
        if nx and k in self.data:
            return False
        self.data[k], self.ttl[k] = v, ex
        return True

    def expire(self, k, s):
        if self.up and k in self.data:
            self.ttl[k] = s

    def delete(self, k):
        if self.up:
            self.data.pop(k, None)


@pytest.fixture
def stores(monkeypatch):
    pg: dict = {}
    redis = FakeRedis()
    monkeypatch.setattr(
        sessions, "_pg_save", lambda sid, st: pg.__setitem__(sid, json.loads(json.dumps(st)))
    )
    monkeypatch.setattr(sessions, "_pg_load", lambda sid: pg.get(sid))
    monkeypatch.setattr(sessions, "get_redis", lambda: redis)
    return pg, redis


def test_create_writes_both_stores(stores):
    pg, redis = stores
    sid = sessions.create_session()
    assert sid in pg and f"session:{sid}" in redis.data
    assert redis.ttl[f"session:{sid}"] == sessions.SESSION_TTL_S
    assert sessions.load(sid) == (sessions.new_state(), "redis")


def test_redis_miss_falls_back_to_postgres_and_repopulates(stores):
    pg, redis = stores
    sid = sessions.create_session()
    redis.data.clear()  # e.g. Redis restarted (no persistence) or key evicted
    state, source = sessions.load(sid)
    assert source == "postgres" and state == sessions.new_state()
    assert f"session:{sid}" in redis.data


def test_works_with_redis_down(stores):
    pg, redis = stores
    redis.up = False
    sid = sessions.create_session()
    assert sessions.load(sid) == (sessions.new_state(), "postgres")
    assert sessions.acquire_lock(sid) is True  # degraded: lock skipped, not blocked


def test_unknown_session(stores):
    assert sessions.load("nope") == (None, "missing")


def test_lock_blocks_concurrent_runs(stores):
    sid = sessions.create_session()
    assert sessions.acquire_lock(sid) is True
    assert sessions.acquire_lock(sid) is False
    sessions.release_lock(sid)
    assert sessions.acquire_lock(sid) is True


def result(status, answer="a", clarification=None, evidence=None):
    return SimpleNamespace(
        status=status,
        answer=answer,
        clarification=clarification,
        evidence=evidence or {},
        citation_report={"checks": []},
    )


def test_clarification_is_stored_then_cleared_by_followup():
    state = sessions.new_state()
    clar = {"question": "Children or adults?", "options": ["children", "adults"]}
    sessions.record_turn(
        state, 1, "How much exercise do I need?", result("clarification", clarification=clar)
    )
    assert state["pending_clarification"]["original_question"] == "How much exercise do I need?"
    sessions.record_turn(state, 2, "adults", result("answered", evidence={"pag:56": "150 min"}))
    assert state["pending_clarification"] is None
    assert [t["question"] for t in state["turns"]] == ["How much exercise do I need?", "adults"]
    assert state["evidence"] == {"pag:56": "150 min"}


def test_repeated_clarification_keeps_original_question():
    state = sessions.new_state()
    clar = {"question": "q?", "options": ["a", "b"]}
    sessions.record_turn(state, 1, "original", result("clarification", clarification=clar))
    sessions.record_turn(state, 2, "hmm", result("clarification", clarification=clar))
    assert state["pending_clarification"]["original_question"] == "original"
