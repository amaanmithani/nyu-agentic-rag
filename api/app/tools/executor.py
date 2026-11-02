"""Runs one tool call safely: validate -> (inject fault) -> run with timeout -> bounded retry.

Guarantees for the agent loop:
  * always returns a ToolResult, never raises;
  * each attempt is capped at timeout_s, and never runs past the run's deadline;
  * only transient failures (timeouts, DB connection errors, injected faults) are retried,
    at most max_retries times with exponential backoff; bad arguments are not retried
    because the same call would fail the same way.

Fault injection (eval case 8 / demos): FAULT_INJECT="search_documents:timeout:1" makes the
first attempt of search_documents hang past the timeout; ":error:3" makes three
consecutive attempts raise a transient error. Several specs can be comma-separated.
A FaultPlan is created per run, so each run sees the faults fresh.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

import psycopg
from pydantic import ValidationError

from app.tools.base import ToolResult
from app.tools.definitions import TOOLS

log = logging.getLogger(__name__)

BACKOFF_BASE_S = 0.25
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")


class InjectedFault(Exception):
    """Simulated transient failure (e.g. a dropped DB connection)."""


TRANSIENT = (FutureTimeout, InjectedFault, psycopg.OperationalError)


class FaultPlan:
    def __init__(self, spec: str = ""):
        self.remaining: dict[str, tuple[str, int]] = {}
        for part in filter(None, (p.strip() for p in spec.split(","))):
            tool, mode, count = part.split(":")
            if mode not in ("timeout", "error"):
                raise ValueError(f"bad fault mode {mode!r} in {part!r}")
            self.remaining[tool] = (mode, int(count))

    def take(self, tool: str) -> str | None:
        """Returns the fault mode to apply to this attempt, or None."""
        mode, count = self.remaining.get(tool, (None, 0))
        if not count:
            return None
        self.remaining[tool] = (mode, count - 1)
        return mode


def format_validation_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "arguments"
        parts.append(f"{loc}: {err['msg']}")
    return "invalid arguments: " + "; ".join(parts)


def execute(
    name: str,
    raw_args: dict,
    *,
    timeout_s: float,
    max_retries: int,
    faults: FaultPlan | None = None,
    deadline: float | None = None,  # time.monotonic() value the run must finish by
) -> ToolResult:
    t0 = time.monotonic()

    def done(result: ToolResult, attempts: int) -> ToolResult:
        result.attempts = attempts
        result.ms = int((time.monotonic() - t0) * 1000)
        return result

    tool = TOOLS.get(name)
    if tool is None:
        return done(ToolResult(status="error", note=f"unknown tool {name!r}"), 0)
    try:
        args = tool.input_model.model_validate(raw_args or {})
    except ValidationError as e:
        return done(ToolResult(status="error", note=format_validation_error(e)), 0)

    last_failure = ""
    attempts = 0
    for attempt in range(max_retries + 1):
        budget = timeout_s
        if deadline is not None:
            budget = min(budget, deadline - time.monotonic())
            if budget <= 0.05:
                break
        attempts += 1
        fault = faults.take(name) if faults else None
        try:
            future = _pool.submit(_run, tool.fn, args, fault, budget)
            return done(future.result(timeout=budget), attempts)
        except FutureTimeout:
            last_failure = f"timed out after {budget:.1f}s"
        except TRANSIENT as e:
            last_failure = f"transient error: {e}"
        except Exception as e:  # a bug in the tool: report it, don't retry, don't crash
            log.exception("tool %s crashed", name)
            note = f"internal tool error ({type(e).__name__})"
            return done(ToolResult(status="error", note=note), attempts)
        log.warning("tool %s attempt %d failed: %s", name, attempts, last_failure)
        if attempt < max_retries:
            time.sleep(BACKOFF_BASE_S * 2**attempt)

    if not attempts:
        note = "run deadline reached before the tool could be called"
        return done(ToolResult(status="timeout", note=note), 0)
    status = "timeout" if last_failure.startswith("timed out") else "error"
    note = f"failed after {attempts} attempt(s); last: {last_failure}"
    return done(ToolResult(status=status, note=note), attempts)


def _run(fn, args, fault: str | None, budget: float) -> ToolResult:
    if fault == "timeout":
        # Really exceed the timeout so the real timeout path runs. The caller has given up
        # on this attempt by then, so don't run the tool afterwards.
        time.sleep(budget + 0.5)
        return ToolResult(status="timeout", note="simulated hang (result discarded)")
    if fault == "error":
        raise InjectedFault("injected fault (simulated connection drop)")
    return fn(args)
