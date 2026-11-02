"""The agent loop, written by hand (no framework) so every decision point is visible.

    PLAN    the model decomposes the question (visible as `purpose` on each search)
    ACT     the model calls one or more tools
    OBSERVE each tool returns a structured ToolResult (ok / empty / weak / error / timeout)
    DECIDE  the model reads the statuses and either rephrases, reads more pages, asks for
            clarification, or answers. The code never decides *what* to retrieve; it only
            enforces the stopping conditions below.

Stopping conditions, checked before every LLM call:
    answer         the model replied without tool calls              -> status "answered"
    clarification  the model called ask_clarification (terminal)     -> status "clarification"
    step_limit     MAX_STEPS LLM calls used                          -> wrap-up -> "partial"
    token_budget   MAX_TOKENS_PER_RUN tokens used                    -> wrap-up -> "partial"
    deadline       RUN_DEADLINE_S minus a reserve for the wrap-up    -> wrap-up -> "partial"
    llm_error      the LLM API failed after the SDK's bounded retries -> evidence-only partial

On a limit, one last call (tools disabled) writes the best partial answer from the evidence
gathered so far plus a "Not completed:" list. If even that fails, the code builds a partial
answer itself from the evidence, so the user always gets something useful.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.agent import citations
from app.agent.llm import LLM, LLMError, Usage
from app.agent.prompts import (
    SYSTEM_PROMPT,
    WRAP_UP_NOTICE,
    format_tool_result,
    initial_user_message,
)
from app.config import Settings
from app.tools.definitions import TOOLS
from app.tools.executor import FaultPlan, execute

Emit = Callable[[str, dict], None]  # (event_type, payload) -> trace / SSE


@dataclass
class RunInput:
    question: str
    prior_turns: list[dict] = field(default_factory=list)
    clarification: dict | None = None  # pending clarification this message answers
    fault_inject: str | None = None  # per-request override of FAULT_INJECT (eval/demo)


@dataclass
class RunResult:
    status: str  # answered | clarification | partial | error
    answer: str
    stop_reason: str
    clarification: dict | None
    citation_report: dict
    evidence: dict[str, str]  # "doc:page" -> text seen this run
    steps: int
    tool_calls: int
    usage: Usage
    cost_usd: float
    latency_ms: int
    model: str


def wrap_up_reserve(deadline_s: float) -> float:
    """Seconds kept back from the deadline so the partial-answer call can still run."""
    return min(15.0, deadline_s * 0.25)


def run_agent(inp: RunInput, llm: LLM, settings: Settings, emit: Emit) -> RunResult:
    t0 = time.monotonic()
    deadline = t0 + settings.run_deadline_s
    work_deadline = deadline - wrap_up_reserve(settings.run_deadline_s)
    faults = FaultPlan(inp.fault_inject if inp.fault_inject is not None else settings.fault_inject)
    tools = [t.anthropic_schema() for t in TOOLS.values()]

    messages: list[dict] = [
        {
            "role": "user",
            "content": initial_user_message(inp.question, inp.prior_turns, inp.clarification),
        }
    ]
    usage = Usage()
    evidence: dict[str, str] = {}
    steps = tool_calls = 0
    answer = ""
    status = stop_reason = ""
    clarification: dict | None = None
    models_used: set[str] = set()

    emit(
        "run_started",
        {
            "question": inp.question,
            "is_followup": inp.clarification is not None,
            "limits": {
                "max_steps": settings.max_steps,
                "max_tokens": settings.max_tokens_per_run,
                "deadline_s": settings.run_deadline_s,
                "tool_timeout_s": settings.tool_timeout_s,
                "tool_max_retries": settings.tool_max_retries,
            },
            "fault_inject": faults.remaining or None,
        },
    )

    while True:
        # ---- stopping conditions (checked before spending another LLM call)
        if steps >= settings.max_steps:
            stop_reason = "step_limit"
        elif usage.total >= settings.max_tokens_per_run:
            stop_reason = "token_budget"
        elif time.monotonic() >= work_deadline:
            stop_reason = "deadline"
        if stop_reason:
            break

        # ---- PLAN / DECIDE: one model turn
        steps += 1
        try:
            resp = llm.create(
                SYSTEM_PROMPT, messages, tools, timeout_s=work_deadline - time.monotonic()
            )
        except LLMError as e:
            stop_reason, status = "llm_error", "error"
            emit("llm_error", {"step": steps, "error": str(e)})
            break
        usage.add(resp.usage)
        models_used.add(resp.model)
        messages.append({"role": "assistant", "content": resp.content})
        emit(
            "llm_call",
            {
                "step": steps,
                "model": resp.model,
                "stop_reason": resp.stop_reason,
                "ms": resp.ms,
                "tokens": resp.usage.as_dict(),
                "tool_calls": [u.name for u in resp.tool_uses],
            },
        )

        if resp.stop_reason == "refusal":
            stop_reason, status, answer = "refusal", "error", "The model declined this request."
            break
        if not resp.tool_uses:
            stop_reason, status, answer = "answer", "answered", resp.text
            break

        # ---- ACT + OBSERVE: run every tool the model asked for
        result_blocks = []
        for use in resp.tool_uses:
            tool_calls += 1
            emit("tool_call", {"step": steps, "id": use.id, "tool": use.name, "args": use.input})
            result = execute(
                use.name,
                use.input,
                timeout_s=settings.tool_timeout_s,
                max_retries=settings.tool_max_retries,
                faults=faults,
                deadline=work_deadline,
            )
            for item in result.data if isinstance(result.data, list) else []:
                if isinstance(item, dict) and "doc_id" in item and "text" in item:
                    key = f"{item['doc_id']}:{item['page']}"
                    evidence[key] = (evidence.get(key, "") + "\n" + item["text"]).strip()
            text, flagged = format_tool_result(use.name, result)
            result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": text,
                    "is_error": result.status in ("error", "timeout"),
                }
            )
            emit(
                "tool_result",
                {
                    "step": steps,
                    "id": use.id,
                    "tool": use.name,
                    "status": result.status,
                    "summary": result.summary(),
                    "citations": [c.key() for c in result.citations],
                    "attempts": result.attempts,
                    "ms": result.ms,
                    "injection_flags": flagged,
                },
            )
            if result.terminal:
                clarification = result.data
        messages.append({"role": "user", "content": result_blocks})

        if clarification:
            stop_reason, status = "clarification", "clarification"
            answer = clarification["question"]
            emit("clarification", clarification)
            break

    # ---- limit reached: ask for a partial answer with tools disabled
    if stop_reason in ("step_limit", "token_budget", "deadline"):
        emit("limit_reached", {"reason": stop_reason, "steps": steps, "tokens": usage.total})
        status = "partial"
        answer = _wrap_up(llm, messages, tools, stop_reason, deadline, usage, models_used, emit)
    llm_failed = stop_reason == "llm_error"
    if llm_failed or (status == "partial" and not answer):
        status = "partial" if evidence else "error"
        answer = _evidence_only_answer(stop_reason, evidence)

    report = citations.summarize(citations.verify(answer, evidence))
    # Priced at the configured model's rates; a refusal fallback (rare) may differ slightly.
    model = ",".join(sorted(models_used)) or llm.model
    result = RunResult(
        status=status,
        answer=answer,
        stop_reason=stop_reason,
        clarification=clarification,
        citation_report=report,
        evidence=evidence,
        steps=steps,
        tool_calls=tool_calls,
        usage=usage,
        cost_usd=usage.cost_usd(llm.model),
        latency_ms=int((time.monotonic() - t0) * 1000),
        model=model,
    )
    emit(
        "run_finished",
        {
            "status": status,
            "stop_reason": stop_reason,
            "answer": answer,
            "citations": report,
            "steps": steps,
            "tool_calls": tool_calls,
            "tokens": usage.as_dict(),
            "cost_usd": result.cost_usd,
            "latency_ms": result.latency_ms,
            "model": model,
        },
    )
    return result


def _wrap_up(llm, messages, tools, reason, deadline, usage, models_used, emit) -> str:
    notice = {"type": "text", "text": WRAP_UP_NOTICE.format(reason=reason.replace("_", " "))}
    last = messages[-1]
    if last["role"] == "user" and isinstance(last["content"], list):
        last["content"].append(notice)  # same turn as the last tool results
    else:
        messages.append({"role": "user", "content": [notice]})
    try:
        resp = llm.create(
            SYSTEM_PROMPT,
            messages,
            tools,
            allow_tools=False,
            timeout_s=max(deadline - time.monotonic(), 5.0),
        )
    except LLMError as e:
        emit("llm_error", {"step": "wrap_up", "error": str(e)})
        return ""
    usage.add(resp.usage)
    models_used.add(resp.model)
    emit(
        "llm_call",
        {
            "step": "wrap_up",
            "model": resp.model,
            "stop_reason": resp.stop_reason,
            "ms": resp.ms,
            "tokens": resp.usage.as_dict(),
            "tool_calls": [],
        },
    )
    return resp.text


def _evidence_only_answer(reason: str, evidence: dict[str, str]) -> str:
    if not evidence:
        return (
            f"I could not complete this request ({reason.replace('_', ' ')}) and no "
            "evidence was retrieved before it stopped. Please try again."
        )
    lines = [
        f"I could not complete this request ({reason.replace('_', ' ')}). "
        "Evidence retrieved before it stopped:"
    ]
    for key, text in list(evidence.items())[:5]:
        lines.append(f"- {text[:200].strip()}... [{key}]")
    lines.append("Not completed: the final answer could not be generated.")
    return "\n".join(lines)
