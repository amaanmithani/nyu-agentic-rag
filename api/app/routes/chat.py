"""Sessions and questions.

POST /sessions                          -> {"session_id"}
GET  /sessions/{sid}                    -> session state (+ whether it came from Redis/Postgres)
POST /sessions/{sid}/ask                -> Server-Sent Events: one event per trace step, then
                                           "done" (or JSON with ?stream=false, used by the eval)
GET  /sessions/{sid}/runs[/{run_id}]    -> saved runs and their full traces

The agent runs in a worker thread and pushes events into a queue; the response generator
drains the queue as SSE. Every event is also written to trace_events before it is sent, so
the trace survives a dropped connection or a restart.
"""

import json
import logging
import queue
import threading
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import runs, sessions
from app.agent.llm import LLMError, make_llm
from app.agent.loop import RunInput, run_agent
from app.baseline.run import run_baseline
from app.config import get_settings
from app.tools.executor import FaultPlan

log = logging.getLogger(__name__)
router = APIRouter()


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    # Debug/eval only, rejected unless ALLOW_FAULT_INJECTION=true. e.g. "search_documents:timeout:1"
    fault_inject: str | None = Field(default=None, max_length=200)
    # "baseline" = single-pass retrieve-once/generate-once, for the eval comparison.
    mode: Literal["agent", "baseline"] = "agent"


@router.post("/sessions", status_code=201)
def create_session():
    return {"session_id": sessions.create_session()}


def _load_or_404(sid: str) -> tuple[dict, str]:
    state, source = sessions.load(sid)
    if state is None:
        raise HTTPException(404, "unknown session")
    return state, source


@router.get("/sessions/{sid}")
def get_session(sid: str):
    state, source = _load_or_404(sid)
    return {"session_id": sid, "state_source": source, "state": state}


@router.get("/sessions/{sid}/runs")
def session_runs(sid: str):
    _load_or_404(sid)
    return {"runs": runs.list_runs(sid)}


@router.get("/sessions/{sid}/runs/{run_id}")
def session_run(sid: str, run_id: int):
    run = runs.get_run(run_id, sid)
    if run is None:
        raise HTTPException(404, "unknown run for this session")
    return run


@router.post("/sessions/{sid}/ask")
def ask(sid: str, body: AskBody, stream: Annotated[bool, Query()] = True):
    settings = get_settings()
    state, _ = _load_or_404(sid)

    if body.fault_inject:
        if not settings.allow_fault_injection:
            raise HTTPException(400, "fault injection is disabled (ALLOW_FAULT_INJECTION=false)")
        try:
            FaultPlan(body.fault_inject)
        except ValueError as e:
            raise HTTPException(400, f"bad fault_inject: {e}") from e
    try:
        llm = make_llm()
    except LLMError as e:
        raise HTTPException(503, str(e)) from e
    if not sessions.acquire_lock(sid):
        raise HTTPException(409, "a question is already running in this session")

    run_id = runs.create_run(sid, body.mode, body.question)
    events: queue.Queue = queue.Queue()
    seq = 0
    tool_results: list[dict] = []

    def emit(kind: str, payload: dict) -> None:
        nonlocal seq
        seq += 1
        runs.record_event(run_id, seq, kind, payload)
        if kind == "tool_result":
            tool_results.append({k: payload[k] for k in ("tool", "status", "summary")})
        events.put((kind, payload))

    def work() -> None:
        try:
            inp = RunInput(
                body.question,
                prior_turns=state["turns"],
                clarification=state["pending_clarification"],
                fault_inject=body.fault_inject,
            )
            runner = run_agent if body.mode == "agent" else run_baseline
            result = runner(inp, llm, settings, emit)
            runs.finish_run(run_id, result)
            sessions.record_turn(state, run_id, body.question, result)
            state["last_tool_results"] = tool_results
            sessions.save(sid, state)
            events.put(
                (
                    "done",
                    {
                        "run_id": run_id,
                        "status": result.status,
                        "pending_clarification": state["pending_clarification"],
                    },
                )
            )
        except Exception as e:  # never leave a run 'running' or the lock held
            log.exception("run %s failed", run_id)
            runs.fail_run(run_id, f"{type(e).__name__}: {e}")
            events.put(("error", {"run_id": run_id, "error": type(e).__name__}))
        finally:
            sessions.release_lock(sid)
            events.put(None)

    if not stream:
        work()
        collected = []
        while (item := events.get()) is not None:
            collected.append({"type": item[0], "data": item[1]})
        return {"run_id": run_id, "events": collected}

    def sse():
        yield _sse("run_created", {"run_id": run_id, "session_id": sid})
        while (item := events.get()) is not None:
            yield _sse(*item)

    threading.Thread(target=work, daemon=True, name=f"run-{run_id}").start()
    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(kind: str, payload: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"
