"""Local embeddings with fastembed (ONNX, CPU). Model id comes from EMBEDDING_MODEL.

Query embeddings are cached in Redis (emb:{sha256(query)}, 7 days): the agent often
re-issues similar searches, and embedding is the slowest part of a cache-miss search.
"""

import hashlib
import json
import threading

from app.config import get_settings
from app.redis import get_redis

DIM = 384
EMB_TTL_S = 7 * 24 * 3600
# bge-*-v1.5 retrieval instruction, recommended for short queries (not for passages).
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_model = None
_reranker = None
_lock = threading.Lock()


def _get_model():
    global _model
    with _lock:  # first call downloads/loads the model; don't do it twice concurrently
        if _model is None:
            from fastembed import TextEmbedding

            _model = TextEmbedding(get_settings().embedding_model)
    return _model


def _get_reranker():
    global _reranker
    with _lock:
        if _reranker is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            _reranker = TextCrossEncoder(get_settings().rerank_model)
    return _reranker


def warm_up() -> None:
    """Load both models at startup so the first search isn't a multi-second cold start."""
    _get_model()
    _get_reranker()


def rerank(query: str, texts: list[str]) -> list[float]:
    if not texts:
        return []
    return [float(s) for s in _get_reranker().rerank(query, texts)]


def embed_passages(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in _get_model().embed(texts, batch_size=32)]


def embed_query(query: str) -> list[float]:
    key = "emb:" + hashlib.sha256(query.encode()).hexdigest()
    r = get_redis()
    cached = r.get(key)
    if cached:
        return json.loads(cached)
    vec = next(iter(_get_model().embed([QUERY_PREFIX + query]))).tolist()
    r.set(key, json.dumps(vec), ex=EMB_TTL_S)
    return vec


def to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
