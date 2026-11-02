"""Upload -> parse -> chunk -> embed -> store.

Split in two so uploads return immediately:
  register_upload()   fast, in the request: validate, dedupe by sha256, save file, insert row
  process_document()  slow, in a FastAPI background task: parse/chunk/embed/insert

Job status lives in Redis (ingest:{doc_id}, 1h TTL) for cheap polling, and the
authoritative status is also kept in documents.status, so polling still works when
Redis is down. Duplicate uploads are prevented by the UNIQUE(sha256) constraint in
Postgres, which holds even without Redis.
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.db import get_pool
from app.ingest.chunker import chunk_pages
from app.ingest.embedder import embed_passages, to_pgvector
from app.ingest.pdf import parse_pdf
from app.redis import get_redis

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_DOCUMENTS = 30
STATUS_TTL_S = 3600


class UploadRejected(ValueError):
    pass


@dataclass
class Registered:
    doc_id: str
    status: str
    duplicate: bool


def slugify(filename: str) -> str:
    stem = Path(filename).stem.lower()
    return re.sub(r"[^a-z0-9]+", "-", stem).strip("-")[:60] or "document"


def set_job_status(doc_id: str, **fields) -> None:
    get_redis().set(f"ingest:{doc_id}", json.dumps(fields), ex=STATUS_TTL_S)


def get_job_status(doc_id: str) -> dict | None:
    cached = get_redis().get(f"ingest:{doc_id}")
    if cached:
        return json.loads(cached) | {"source": "redis"}
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, page_count, chunk_count, error FROM documents WHERE id = %s",
            (doc_id,),
        ).fetchone()
    return (row | {"source": "postgres"}) if row else None


def register_upload(data: bytes, filename: str) -> Registered:
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadRejected(f"{filename}: larger than {MAX_UPLOAD_BYTES // 2**20} MB")
    if not data.startswith(b"%PDF-"):
        raise UploadRejected(f"{filename}: not a PDF")

    sha = hashlib.sha256(data).hexdigest()
    with get_pool().connection() as conn:
        existing = conn.execute(
            "SELECT id, status FROM documents WHERE sha256 = %s", (sha,)
        ).fetchone()
        if existing and existing["status"] != "failed":
            return Registered(existing["id"], existing["status"], duplicate=True)
        if existing:  # a previous attempt failed: start over
            conn.execute("DELETE FROM documents WHERE id = %s", (existing["id"],))

        count = conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"]
        if count >= MAX_DOCUMENTS:
            raise UploadRejected(f"corpus limit of {MAX_DOCUMENTS} documents reached")

        base = slugify(filename)
        doc_id, n = base, 1
        while conn.execute("SELECT 1 FROM documents WHERE id = %s", (doc_id,)).fetchone():
            n += 1
            doc_id = f"{base}-{n}"
        conn.execute(
            "INSERT INTO documents (id, title, filename, sha256) VALUES (%s, %s, %s, %s)",
            (doc_id, Path(filename).stem, filename, sha),
        )

    path = Path(get_settings().upload_dir) / f"{doc_id}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    set_job_status(doc_id, status="processing", stage="queued")
    return Registered(doc_id, "processing", duplicate=False)


def process_document(doc_id: str) -> None:
    t0 = time.monotonic()
    try:
        data = (Path(get_settings().upload_dir) / f"{doc_id}.pdf").read_bytes()
        set_job_status(doc_id, status="processing", stage="parsing")
        parsed = parse_pdf(data)
        chunks = chunk_pages(parsed.pages)
        if not chunks:
            raise ValueError("no extractable text (scanned PDF? OCR is not supported)")

        set_job_status(doc_id, status="processing", stage="embedding", chunks=len(chunks))
        vectors = embed_passages([c.text for c in chunks])

        with get_pool().connection() as conn, conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO pages (doc_id, page, text) VALUES (%s, %s, %s)",
                    [(doc_id, p, t) for p, t in parsed.pages],
                )
                cur.executemany(
                    "INSERT INTO chunks (doc_id, page, chunk_idx, text, embedding) "
                    "VALUES (%s, %s, %s, %s, %s::vector)",
                    [
                        (doc_id, c.page, c.chunk_idx, c.text, to_pgvector(v))
                        for c, v in zip(chunks, vectors, strict=True)
                    ],
                )
            conn.execute(
                "UPDATE documents SET status = 'ready', page_count = %s, chunk_count = %s, "
                "title = COALESCE(%s, title) WHERE id = %s",
                (len(parsed.pages), len(chunks), parsed.title, doc_id),
            )
            # Invalidates every cached search result (the version is part of the key).
            conn.execute("UPDATE corpus_state SET version = version + 1")

        secs = round(time.monotonic() - t0, 1)
        set_job_status(
            doc_id, status="ready", pages=len(parsed.pages), chunks=len(chunks), seconds=secs
        )
        log.info(
            "ingested %s: %d pages, %d chunks in %ss", doc_id, len(parsed.pages), len(chunks), secs
        )
    except Exception as e:
        log.exception("ingest failed for %s", doc_id)
        with get_pool().connection() as conn:
            conn.execute(
                "UPDATE documents SET status = 'failed', error = %s WHERE id = %s",
                (str(e)[:500], doc_id),
            )
        set_job_status(doc_id, status="failed", error=str(e)[:500])


def delete_document(doc_id: str) -> bool:
    with get_pool().connection() as conn, conn.transaction():
        deleted = conn.execute("DELETE FROM documents WHERE id = %s", (doc_id,)).rowcount
        if deleted:
            conn.execute("UPDATE corpus_state SET version = version + 1")
    (Path(get_settings().upload_dir) / f"{doc_id}.pdf").unlink(missing_ok=True)
    get_redis().delete(f"ingest:{doc_id}")
    return bool(deleted)
