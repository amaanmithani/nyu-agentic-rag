"""Read-only corpus access for tools. This module is the tools' entire view of the world.

Tools cannot reach files, the network, or any table except documents/pages (and chunks via
retrieval.hybrid_search). Every query runs inside a READ ONLY transaction, so Postgres
itself rejects writes even if a bug tried one.
"""

from app.db import get_pool


def _read(sql: str, params: tuple = ()) -> list[dict]:
    with get_pool().connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        return conn.execute(sql, params).fetchall()


def list_ready_documents() -> list[dict]:
    return _read("SELECT id, title, page_count FROM documents WHERE status = 'ready' ORDER BY id")


def ready_doc_ids() -> set[str]:
    return {d["id"] for d in list_ready_documents()}


def get_document(doc_id: str) -> dict | None:
    rows = _read(
        "SELECT id, title, page_count FROM documents WHERE id = %s AND status = 'ready'",
        (doc_id,),
    )
    return rows[0] if rows else None


def get_pages(doc_id: str, page_start: int, page_end: int) -> list[dict]:
    return _read(
        "SELECT page, text FROM pages WHERE doc_id = %s AND page BETWEEN %s AND %s ORDER BY page",
        (doc_id, page_start, page_end),
    )
