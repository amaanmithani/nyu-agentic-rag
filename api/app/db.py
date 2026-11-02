"""Postgres access: one connection pool, plain SQL via psycopg.

Plain SQL (no ORM) keeps the pgvector / tsvector queries readable and explicit.
"""

from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

SCHEMA = Path(__file__).with_name("schema.sql")

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            get_settings().database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def init_schema() -> None:
    with get_pool().connection() as conn:
        conn.execute(SCHEMA.read_text())


def ping() -> bool:
    try:
        with get_pool().connection(timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def corpus_version() -> int:
    with get_pool().connection() as conn:
        return conn.execute("SELECT version FROM corpus_state").fetchone()["version"]


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
