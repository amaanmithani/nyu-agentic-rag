"""Single-pass baseline: retrieve once, generate once. Used for the eval comparison.

Kept as close to the agent as possible so the comparison isolates the *loop*:
  * same retrieval: one search_documents call through the same executor (same hybrid search,
    reranker, top_k, timeout/retry and fault injection);
  * same model, same system prompt, same <document_content> wrapping and citation checks;
  * tools are listed but disabled (tool_choice "none"), so the request prefix is identical
    and the model is told it must answer from the passages given.
What it cannot do: rephrase a weak query, read neighbouring pages, run one search per
sub-question, or call ask_clarification.
"""

import time

from app.agent import citations
from app.agent.llm import LLM, LLMError, Usage
from app.agent.loop import Emit, RunInput, RunResult
from app.agent.prompts import SYSTEM_PROMPT, format_tool_result, initial_user_message
from app.config import Settings
from app.tools.definitions import TOOLS
from app.tools.executor import FaultPlan, execute

TOP_K = 5  # same default as the agent's search_documents

BASELINE_NOTE = """\
[Single-pass mode: tools are disabled. The passages below were retrieved once for the \
question. Answer from them only, following all the rules above.]"""


def run_baseline(inp: RunInput, llm: LLM, settings: Settings, emit: Emit) -> RunResult:
    t0 = time.monotonic()
    deadline = t0 + settings.run_deadline_s
    faults = FaultPlan(inp.fault_inject if inp.fault_inject is not None else settings.fault_inject)
    emit(
        "run_started",
        {
            "question": inp.question,
            "mode": "baseline",
            "is_followup": False,
            "limits": {
                "max_steps": 1,
                "max_tokens": settings.max_tokens_per_run,
                "deadline_s": settings.run_deadline_s,
                "tool_timeout_s": settings.tool_timeout_s,
                "tool_max_retries": settings.tool_max_retries,
            },
            "fault_inject": faults.remaining or None,
        },
    )

    # ---- retrieve once, with the raw question as the query
    args = {"query": inp.question[:300], "top_k": TOP_K}
    emit(
        "tool_call", {"step": 1, "id": "baseline_search", "tool": "search_documents", "args": args}
    )
    result = execute(
        "search_documents",
        args,
        timeout_s=settings.tool_timeout_s,
        max_retries=settings.tool_max_retries,
        faults=faults,
        deadline=deadline,
    )
    evidence: dict[str, str] = {}
    for item in result.data if isinstance(result.data, list) else []:
        key = f"{item['doc_id']}:{item['page']}"
        evidence[key] = (evidence.get(key, "") + "\n" + item["text"]).strip()
    passages, flagged = format_tool_result("search_documents", result)
    emit(
        "tool_result",
        {
            "step": 1,
            "id": "baseline_search",
            "tool": "search_documents",
            "status": result.status,
            "summary": result.summary(),
            "citations": [c.key() for c in result.citations],
            "attempts": result.attempts,
            "ms": result.ms,
            "injection_flags": flagged,
        },
    )

    # ---- generate once
    user = (
        initial_user_message(inp.question, inp.prior_turns, None)
        + f"\n\n{BASELINE_NOTE}\n\nRetrieved passages ({result.status}):\n{passages}"
    )
    tools = [t.anthropic_schema() for t in TOOLS.values()]
    usage = Usage()
    model = llm.model
    try:
        resp = llm.create(
            SYSTEM_PROMPT,
            [{"role": "user", "content": user}],
            tools,
            allow_tools=False,
            timeout_s=max(deadline - time.monotonic(), 5.0),
        )
        usage.add(resp.usage)
        model = resp.model
        answer, status, stop = resp.text, "answered", "answer"
        if not answer.strip():
            # Seen in eval: with tools disabled, the model sometimes returns no text at all
            # when it would have asked for clarification. Report it instead of "answered".
            answer = "No answer was generated (empty model response)."
            status, stop = "error", "empty_response"
        emit(
            "llm_call",
            {
                "step": 1,
                "model": resp.model,
                "stop_reason": resp.stop_reason,
                "ms": resp.ms,
                "tokens": resp.usage.as_dict(),
                "tool_calls": [],
            },
        )
    except LLMError as e:
        emit("llm_error", {"step": 1, "error": str(e)})
        answer, status, stop = (
            "The answer could not be generated (LLM error).",
            "error",
            "llm_error",
        )

    report = citations.summarize(citations.verify(answer, evidence))
    run = RunResult(
        status=status,
        answer=answer,
        stop_reason=stop,
        clarification=None,
        citation_report=report,
        evidence=evidence,
        steps=1,
        tool_calls=1,
        usage=usage,
        cost_usd=usage.cost_usd(llm.model),
        latency_ms=int((time.monotonic() - t0) * 1000),
        model=model,
    )
    emit(
        "run_finished",
        {
            "status": status,
            "stop_reason": stop,
            "answer": answer,
            "citations": report,
            "steps": 1,
            "tool_calls": 1,
            "tokens": usage.as_dict(),
            "cost_usd": run.cost_usd,
            "latency_ms": run.latency_ms,
            "model": model,
        },
    )
    return run
