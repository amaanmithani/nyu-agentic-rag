import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import db
from app.redis import get_redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    yield
    db.close_pool()


app = FastAPI(title="Agentic RAG API", lifespan=lifespan)


@app.get("/health")
def health():
    """Postgres is required (503 if down). Redis is optional ("degraded" if down)."""
    db_ok = db.ping()
    redis_ok = get_redis().ping()
    body = {
        "status": "ok" if db_ok and redis_ok else ("degraded" if db_ok else "down"),
        "db": "ok" if db_ok else "down",
        "redis": "ok" if redis_ok else "degraded",
    }
    return JSONResponse(body, status_code=200 if db_ok else 503)
