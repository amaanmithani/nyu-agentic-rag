"""Persistence for runs and trace events (Postgres). Used by the agent route and the baseline."""

import json
import logging

from psycopg.types.json import Jsonb

from app.db import get_pool

log = logging.getLogger("runs")


def create_run(session_id: str | None, mode: str, question: str) -> int:
    with get_pool().connection() as conn:
        return conn.execute(
            "INSERT INTO runs (session_id, mode, question) VALUES (%s, %s, %s) RETURNING id",
            (session_id, mode, question),
        ).fetchone()["id"]


def record_event(run_id: int, seq: int, kind: str, payload: dict) -> None:
    step = payload.get("step")
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO trace_events (run_id, seq, type, step, tool, status, ms, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run_id,
                seq,
                kind,
                None if step is None else str(step),
                payload.get("tool"),
                payload.get("status"),
                payload.get("ms") or payload.get("latency_ms"),
                Jsonb(payload),
            ),
        )


def finish_run(run_id: int, result) -> None:
    u = result.usage
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE runs SET status=%s, stop_reason=%s, answer=%s, citation_report=%s, steps=%s, "
            "tool_calls=%s, tokens_in=%s, tokens_out=%s, tokens_cache_read=%s, "
            "tokens_cache_write=%s, cost_usd=%s, latency_ms=%s, model=%s, finished_at=now() "
            "WHERE id=%s",
            (
                result.status,
                result.stop_reason,
                result.answer,
                Jsonb(result.citation_report),
                result.steps,
                result.tool_calls,
                u.input_tokens,
                u.output_tokens,
                u.cache_read_tokens,
                u.cache_write_tokens,
                result.cost_usd,
                result.latency_ms,
                result.model,
                run_id,
            ),
        )
    # One structured log line per run: greppable with `docker compose logs api | grep run_done`.
    log.info(
        "run_done %s",
        json.dumps(
            {
                "run_id": run_id,
                "status": result.status,
                "stop_reason": result.stop_reason,
                "steps": result.steps,
                "tool_calls": result.tool_calls,
                "tokens": u.total,
                "cost_usd": result.cost_usd,
                "latency_ms": result.latency_ms,
            }
        ),
    )


def fail_run(run_id: int, error: str) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE runs SET status='error', stop_reason='server_error', answer=%s, "
            "finished_at=now() WHERE id=%s",
            (error[:500], run_id),
        )


def get_run(run_id: int, session_id: str) -> dict | None:
    """Scoped by session: a run is only visible through the session that created it."""
    with get_pool().connection() as conn:
        run = conn.execute(
            "SELECT * FROM runs WHERE id = %s AND session_id = %s", (run_id, session_id)
        ).fetchone()
        if run is None:
            return None
        run["events"] = conn.execute(
            "SELECT seq, type, payload, created_at FROM trace_events WHERE run_id = %s "
            "ORDER BY seq",
            (run_id,),
        ).fetchall()
    return run


def list_runs(session_id: str) -> list[dict]:
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT id, mode, question, status, stop_reason, steps, tool_calls, cost_usd, "
            "latency_ms, created_at FROM runs WHERE session_id = %s ORDER BY id",
            (session_id,),
        ).fetchall()
