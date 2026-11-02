"""Postgres access: one connection pool, plain SQL via psycopg.

Plain SQL (no ORM) keeps the pgvector / tsvector queries readable and explicit.
"""

from psycopg_pool import ConnectionPool

from app.config import get_settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(get_settings().database_url, min_size=1, max_size=10, open=True)
    return _pool


def init_schema() -> None:
    with get_pool().connection() as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")


def ping() -> bool:
    try:
        with get_pool().connection(timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
