"""Optional shared-password login (enabled when APP_PASSWORD is set).

POST /auth/login {password} -> sets an HttpOnly cookie holding "<expiry>.<hmac>". Every API
route except /health and /auth/* then requires a valid cookie (401 otherwise).

* The signing key is derived from APP_PASSWORD, so every replica verifies tokens without
  shared state, and changing the password invalidates all sessions.
* Brute force: wrong attempts per client IP are counted in Redis (login_fail:{ip}, 5 min TTL);
  after MAX_FAILURES the IP gets 429 until the key expires. If Redis is down the limit is
  skipped (degraded) rather than locking everyone out.
* Passwords and tokens are compared in constant time.
"""

import hashlib
import hmac
import time

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.redis import get_redis

COOKIE = "rag_auth"
TOKEN_TTL_S = 12 * 3600
MAX_FAILURES = 5
FAIL_WINDOW_S = 300
PUBLIC_PATHS = ("/health", "/auth/")

router = APIRouter()


def enabled() -> bool:
    return bool(get_settings().app_password)


def _key() -> bytes:
    return hashlib.sha256(b"rag-auth:" + get_settings().app_password.encode()).digest()


def make_token(now: float | None = None) -> str:
    expiry = str(int((now or time.time()) + TOKEN_TTL_S))
    sig = hmac.new(_key(), expiry.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def valid_token(token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    expiry, sig = token.split(".", 1)
    good = hmac.new(_key(), expiry.encode(), hashlib.sha256).hexdigest()
    return (
        hmac.compare_digest(sig, good) and expiry.isdigit() and int(expiry) > (now or time.time())
    )


def _client_ip(request: Request) -> str:
    # nginx sets X-Real-IP to the address the OpenShift router saw (never the client-supplied
    # part of X-Forwarded-For, which would let an attacker dodge the lockout). The API itself
    # is only reachable through nginx in Kubernetes/OpenShift.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


async def require_login(request: Request, call_next):
    """HTTP middleware: lets public paths through, otherwise needs a valid cookie."""
    if not enabled() or request.url.path.startswith(PUBLIC_PATHS):
        return await call_next(request)
    if not valid_token(request.cookies.get(COOKIE)):
        return JSONResponse({"detail": "login required"}, status_code=401)
    return await call_next(request)


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)


@router.get("/auth/status")
def status(request: Request):
    return {
        "required": enabled(),
        "authenticated": not enabled() or valid_token(request.cookies.get(COOKIE)),
    }


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response):
    if not enabled():
        return {"ok": True, "required": False}
    key = f"login_fail:{_client_ip(request)}"
    r = get_redis()
    failures = int(r.get(key) or 0)
    if failures >= MAX_FAILURES:
        raise HTTPException(429, "too many failed attempts; try again in a few minutes")
    if not hmac.compare_digest(body.password.encode(), get_settings().app_password.encode()):
        if r.incr(key) is not None:
            r.expire(key, FAIL_WINDOW_S)
        raise HTTPException(401, "wrong password")
    r.delete(key)
    response.set_cookie(
        COOKIE,
        make_token(),
        max_age=TOKEN_TTL_S,
        httponly=True,
        samesite="strict",
        # Secure when the original request was HTTPS (OpenShift router / nginx set this header).
        secure=request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https",
    )
    return {"ok": True, "required": True}


@router.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}
