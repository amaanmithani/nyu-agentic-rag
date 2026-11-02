"""Hybrid search: pgvector (meaning) + Postgres full-text (exact terms), fused with RRF,
then reranked with a cross-encoder.

Why both retrievers: embeddings catch paraphrases ("exercise" ~ "physical activity") but
are weak on exact figures and names ("2,300 mg", "DASH"); keyword search is the opposite.
Reciprocal Rank Fusion combines the two *rankings* (not their raw scores, which are on
different scales): score = sum over lists of 1 / (RRF_K + rank).

Why rerank: bi-encoder cosine similarities are compressed (an off-topic chunk scores ~0.69,
a relevant one ~0.70), so they can't tell the agent "nothing relevant was found". The
cross-encoder reads query and passage together; on this corpus relevant passages score
> 0 and out-of-corpus queries score < -5. That score drives the tool's "weak" status.

Results are cached in Redis under ret:{corpus_version}:{sha256(query, filters, top_k)}
for 1h. Bumping corpus_version on upload/delete makes old entries unreachable; they then
expire on their own.
"""

import hashlib
import json
from dataclasses import asdict, dataclass

from app.db import corpus_version, get_pool
from app.ingest.embedder import embed_query, rerank, to_pgvector
from app.redis import get_redis

RRF_K = 60
CANDIDATES = 40  # per retriever, before fusion
RERANK_POOL = 20  # fused candidates passed to the cross-encoder (~1-2 s on CPU)
CACHE_TTL_S = 3600


@dataclass
class Hit:
    chunk_id: int
    doc_id: str
    page: int
    chunk_idx: int
    text: str
    rrf_score: float
    vector_sim: float | None  # cosine similarity, None if only found by keyword
    keyword_rank: int | None  # 1-based rank in the keyword list, None if not matched
    rerank_score: float | None = None  # cross-encoder logit; final sort key


VECTOR_SQL = """
SELECT c.id, c.doc_id, c.page, c.chunk_idx, c.text,
       1 - (c.embedding <=> %(vec)s::vector) AS sim
FROM chunks c JOIN documents d ON d.id = c.doc_id
WHERE d.status = 'ready' AND (%(docs)s::text[] IS NULL OR c.doc_id = ANY(%(docs)s))
ORDER BY c.embedding <=> %(vec)s::vector
LIMIT %(n)s
"""

# plainto_tsquery ANDs every word, so a long natural-language question rarely matches
# anything. Rewriting '&' to '|' makes it an OR query; ts_rank_cd still ranks chunks
# that match more (and closer) terms higher.
KEYWORD_SQL = """
WITH q AS (
  SELECT to_tsquery('english', replace(plainto_tsquery('english', %(q)s)::text, '&', '|')) AS tsq
)
SELECT c.id, c.doc_id, c.page, c.chunk_idx, c.text
FROM chunks c JOIN documents d ON d.id = c.doc_id, q
WHERE d.status = 'ready' AND c.tsv @@ q.tsq
  AND (%(docs)s::text[] IS NULL OR c.doc_id = ANY(%(docs)s))
ORDER BY ts_rank_cd(c.tsv, q.tsq) DESC
LIMIT %(n)s
"""


def rrf_fuse(vector_rows: list[dict], keyword_rows: list[dict], top_k: int) -> list[Hit]:
    hits: dict[int, Hit] = {}
    for rank, row in enumerate(vector_rows, start=1):
        hits[row["id"]] = Hit(
            row["id"],
            row["doc_id"],
            row["page"],
            row["chunk_idx"],
            row["text"],
            1 / (RRF_K + rank),
            round(row["sim"], 4),
            None,
        )
    for rank, row in enumerate(keyword_rows, start=1):
        hit = hits.get(row["id"])
        if hit is None:
            hit = hits[row["id"]] = Hit(
                row["id"],
                row["doc_id"],
                row["page"],
                row["chunk_idx"],
                row["text"],
                0.0,
                None,
                None,
            )
        hit.rrf_score += 1 / (RRF_K + rank)
        hit.keyword_rank = rank
    ranked = sorted(hits.values(), key=lambda h: h.rrf_score, reverse=True)[:top_k]
    for h in ranked:
        h.rrf_score = round(h.rrf_score, 5)
    return ranked


def _cache_key(query: str, doc_ids: list[str] | None, top_k: int) -> str:
    payload = json.dumps([query.strip().lower(), sorted(doc_ids or []), top_k])
    return f"ret:{corpus_version()}:{hashlib.sha256(payload.encode()).hexdigest()}"


def hybrid_search(query: str, doc_ids: list[str] | None = None, top_k: int = 8) -> dict:
    """Returns {"hits": [...], "cached": bool}. doc_ids=None searches the whole corpus."""
    key = _cache_key(query, doc_ids, top_k)
    r = get_redis()
    cached = r.get(key)
    if cached:
        return {"hits": json.loads(cached), "cached": True}

    params = {"q": query, "vec": to_pgvector(embed_query(query)), "docs": doc_ids, "n": CANDIDATES}
    with get_pool().connection() as conn:
        conn.execute("SET hnsw.ef_search = 100")
        vector_rows = conn.execute(VECTOR_SQL, params).fetchall()
        keyword_rows = conn.execute(KEYWORD_SQL, params).fetchall()

    pool = rrf_fuse(vector_rows, keyword_rows, RERANK_POOL)
    for h, score in zip(pool, rerank(query, [h.text for h in pool]), strict=True):
        h.rerank_score = round(score, 3)
    pool.sort(key=lambda h: h.rerank_score, reverse=True)
    hits = [asdict(h) for h in pool[:top_k]]
    r.set(key, json.dumps(hits), ex=CACHE_TTL_S)
    return {"hits": hits, "cached": False}
