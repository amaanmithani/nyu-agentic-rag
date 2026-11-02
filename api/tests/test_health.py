from fastapi.testclient import TestClient

from app import main


def make_client(monkeypatch, db_ok: bool, redis_ok: bool) -> TestClient:
    monkeypatch.setattr(main.db, "init_schema", lambda: None)
    monkeypatch.setattr(main.db, "ping", lambda: db_ok)
    monkeypatch.setattr(main.get_redis(), "ping", lambda: redis_ok)
    return TestClient(main.app)


def test_health_ok(monkeypatch):
    with make_client(monkeypatch, db_ok=True, redis_ok=True) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok", "redis": "ok"}


def test_health_redis_down_is_degraded_not_failing(monkeypatch):
    with make_client(monkeypatch, db_ok=True, redis_ok=False) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["redis"] == "degraded"


def test_health_db_down_is_503(monkeypatch):
    with make_client(monkeypatch, db_ok=False, redis_ok=True) as c:
        r = c.get("/health")
    assert r.status_code == 503
