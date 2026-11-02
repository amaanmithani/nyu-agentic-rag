"""Per-session agent state: Redis fast path, Postgres durable copy.

State shape (JSON):
    {
      "turns": [{"run_id", "question", "answer", "status", "citations": ["doc:page", ...]}],
      "pending_clarification": {"original_question", "question", "options"} | null,
      "evidence": {"doc:page": "first 300 chars of what the tools returned"},
      "last_tool_results": [{"tool", "status", "summary"}]
    }

Keys:
    session:{sid}       state JSON, 24h TTL, sliding (refreshed on every read/write)
    lock:session:{sid}  SET NX EX 120 while a run is in progress (one run per session)

Writes go to Postgres first, then Redis. Reads try Redis, then fall back to Postgres and
re-populate Redis. So Redis being down, flushed, or evicting keys only costs latency.
"""

import json
import secrets

from psycopg.types.json import Jsonb

from app.db import get_pool
from app.redis import get_redis

SESSION_TTL_S = 24 * 3600
LOCK_TTL_S = 120
MAX_TURNS = 20
MAX_EVIDENCE = 40


def new_state() -> dict:
    return {"turns": [], "pending_clarification": None, "evidence": {}, "last_tool_results": []}


def _pg_save(sid: str, state: dict) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO sessions (id, state) VALUES (%s, %s) "
            "ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
            (sid, Jsonb(state)),
        )


def _pg_load(sid: str) -> dict | None:
    with get_pool().connection() as conn:
        row = conn.execute("SELECT state FROM sessions WHERE id = %s", (sid,)).fetchone()
    return row["state"] if row else None


def create_session() -> str:
    sid = secrets.token_urlsafe(16)  # 128 bits: unguessable, so it doubles as the access key
    save(sid, new_state())
    return sid


def save(sid: str, state: dict) -> None:
    state["turns"] = state["turns"][-MAX_TURNS:]
    state["evidence"] = dict(list(state["evidence"].items())[-MAX_EVIDENCE:])
    _pg_save(sid, state)
    get_redis().set(f"session:{sid}", json.dumps(state), ex=SESSION_TTL_S)


def load(sid: str) -> tuple[dict | None, str]:
    """Returns (state or None, source) where source is 'redis' | 'postgres' | 'missing'."""
    r = get_redis()
    cached = r.get(f"session:{sid}")
    if cached:
        r.expire(f"session:{sid}", SESSION_TTL_S)  # sliding expiry
        return json.loads(cached), "redis"
    state = _pg_load(sid)
    if state is None:
        return None, "missing"
    r.set(f"session:{sid}", json.dumps(state), ex=SESSION_TTL_S)
    return state, "postgres"


def acquire_lock(sid: str) -> bool:
    """True if this caller may run. If Redis is unavailable the lock is skipped (degraded)."""
    got = get_redis().set(f"lock:session:{sid}", "1", ex=LOCK_TTL_S, nx=True)
    return got is not False  # None = Redis down -> allow; False = someone holds the lock


def release_lock(sid: str) -> None:
    get_redis().delete(f"lock:session:{sid}")


def record_turn(state: dict, run_id: int, question: str, result) -> None:
    """Fold a finished run into session state (question, evidence, clarification, results)."""
    state["turns"].append(
        {
            "run_id": run_id,
            "question": question,
            "answer": result.answer,
            "status": result.status,
            "citations": sorted({c["key"] for c in result.citation_report["checks"]}),
        }
    )
    for key, text in result.evidence.items():
        state["evidence"][key] = text[:300]
    if result.status == "clarification":
        pending = state.get("pending_clarification")
        # A clarification inside a follow-up keeps pointing at the very first question.
        original = pending["original_question"] if pending else question
        state["pending_clarification"] = {"original_question": original, **result.clarification}
    else:
        state["pending_clarification"] = None
