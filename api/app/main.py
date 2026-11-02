import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import db
from app.ingest import embedder
from app.redis import get_redis
from app.routes import documents

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

models_ready = threading.Event()


def _warm_models() -> None:
    embedder.warm_up()
    models_ready.set()
    logging.getLogger(__name__).info("embedding + rerank models loaded")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    # In a thread: the first start downloads ~200 MB into the model-cache volume, and the
    # container should report healthy (and serve /documents) while that happens.
    threading.Thread(target=_warm_models, daemon=True).start()
    yield
    db.close_pool()


app = FastAPI(title="Agentic RAG API", lifespan=lifespan)
app.include_router(documents.router)


@app.get("/health")
def health():
    """Postgres is required (503 if down). Redis is optional ("degraded" if down)."""
    db_ok = db.ping()
    redis_ok = get_redis().ping()
    body = {
        "status": "ok" if db_ok and redis_ok else ("degraded" if db_ok else "down"),
        "db": "ok" if db_ok else "down",
        "redis": "ok" if redis_ok else "degraded",
        "models": "ready" if models_ready.is_set() else "loading",
    }
    return JSONResponse(body, status_code=200 if db_ok else 503)
