"""Optional login: token signing, protected routes, wrong passwords, Redis-backed lockout."""

import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.config import get_settings


class FakeRedis:
    def __init__(self):
        self.data = {}

    def get(self, k):
        return self.data.get(k)

    def incr(self, k):
        self.data[k] = str(int(self.data.get(k, 0)) + 1)
        return int(self.data[k])

    def expire(self, k, s):
        return True

    def delete(self, k):
        self.data.pop(k, None)

    def ping(self):
        return True


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(get_settings(), "app_password", "s3cret")
    redis = FakeRedis()  # one instance, so failed-attempt counts accumulate across requests
    monkeypatch.setattr(auth, "get_redis", lambda: redis)
    monkeypatch.setattr(main.db, "init_schema", lambda: None)
    monkeypatch.setattr(main.db, "ping", lambda: True)
    monkeypatch.setattr(main.embedder, "warm_up", lambda: None)
    with TestClient(main.app) as c:
        yield c


def test_token_roundtrip_and_expiry(monkeypatch):
    monkeypatch.setattr(get_settings(), "app_password", "s3cret")
    token = auth.make_token(now=1000)
    assert auth.valid_token(token, now=1001)
    assert not auth.valid_token(token, now=1000 + auth.TOKEN_TTL_S + 1)  # expired
    assert not auth.valid_token(token[:-1] + ("0" if token[-1] != "0" else "1"), now=1001)
    monkeypatch.setattr(get_settings(), "app_password", "changed")
    assert not auth.valid_token(token, now=1001)  # password change invalidates sessions


def test_protected_routes_need_login(client):
    assert client.get("/health").status_code == 200  # probes stay public
    assert client.get("/auth/status").json() == {"required": True, "authenticated": False}
    assert client.get("/documents").status_code == 401
    assert client.post("/sessions").status_code == 401


def test_login_sets_cookie_and_unlocks(client):
    r = client.post("/auth/login", json={"password": "s3cret"})
    assert r.status_code == 200 and auth.COOKIE in r.cookies
    assert client.get("/auth/status").json()["authenticated"] is True
    assert client.post("/auth/logout").status_code == 200


def test_wrong_password_then_lockout(client):
    for _ in range(auth.MAX_FAILURES):
        assert client.post("/auth/login", json={"password": "nope"}).status_code == 401
    # locked out even with the right password until the window expires
    assert client.post("/auth/login", json={"password": "s3cret"}).status_code == 429


def test_login_disabled_when_no_password(monkeypatch):
    monkeypatch.setattr(get_settings(), "app_password", "")
    assert auth.enabled() is False
