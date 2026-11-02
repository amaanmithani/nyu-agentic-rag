"""Agent loop tests with a scripted fake LLM and a faked corpus: no API key, no database."""

import pytest

from app.agent.llm import LLMError, LLMResponse, StubLLM, ToolUse, Usage
from app.agent.loop import RunInput, run_agent
from app.config import Settings
from app.tools import corpus, definitions

PAGES = {
    ("sleep", 8): "Research suggests that adults need at least 7-8 hours of sleep each night.",
    (
        "pag-2018",
        56,
    ): "Adults should do at least 150 minutes a week of moderate-intensity activity.",
}


@pytest.fixture(autouse=True)
def fake_corpus(monkeypatch):
    docs = [
        {"id": "sleep", "title": "Sleep", "page_count": 72},
        {"id": "pag-2018", "title": "PAG", "page_count": 118},
    ]
    monkeypatch.setattr(corpus, "list_ready_documents", lambda: docs)
    monkeypatch.setattr(corpus, "ready_doc_ids", lambda: {d["id"] for d in docs})
    monkeypatch.setattr(
        corpus, "get_document", lambda i: next((d for d in docs if d["id"] == i), None)
    )
    monkeypatch.setattr(
        corpus,
        "get_pages",
        lambda d, s, e: [
            {"page": p, "text": PAGES.get((d, p), f"{d} page {p}")} for p in range(s, e + 1)
        ],
    )

    def search(query, doc_ids, top_k):
        if "creatine" in query or "bad wording" in query:
            hits = [{"doc_id": "pag-2018", "page": 61, "rerank_score": -8.0, "text": "muscle"}]
        elif "sleep" in query:
            hits = [
                {"doc_id": "sleep", "page": 8, "rerank_score": 7.2, "text": PAGES[("sleep", 8)]}
            ]
        else:
            hits = [
                {
                    "doc_id": "pag-2018",
                    "page": 56,
                    "rerank_score": 6.0,
                    "text": PAGES[("pag-2018", 56)],
                }
            ]
        return {"hits": hits, "cached": False}

    monkeypatch.setattr(definitions.retrieval, "hybrid_search", search)


class ScriptedLLM:
    """Returns pre-written turns in order and records how it was called."""

    model = "claude-sonnet-5-5"

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def create(self, system, messages, tools, *, allow_tools=True, timeout_s=60.0):
        self.calls.append({"allow_tools": allow_tools, "messages": [dict(m) for m in messages]})
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def tool_turn(*calls: tuple[str, dict]) -> LLMResponse:
    uses = [ToolUse(f"t{i}_{name}", name, args) for i, (name, args) in enumerate(calls)]
    content = [{"type": "tool_use", "id": u.id, "name": u.name, "input": u.input} for u in uses]
    return LLMResponse(
        content,
        "",
        uses,
        "tool_use",
        "claude-sonnet-5-5",
        Usage(input_tokens=1000, output_tokens=100),
    )


def text_turn(text: str) -> LLMResponse:
    return LLMResponse(
        [{"type": "text", "text": text}],
        text,
        [],
        "end_turn",
        "claude-sonnet-5-5",
        Usage(input_tokens=1500, output_tokens=200),
    )


def settings(**kw) -> Settings:
    base = dict(
        max_steps=8,
        tool_timeout_s=2.0,
        tool_max_retries=2,
        max_tokens_per_run=60_000,
        run_deadline_s=60.0,
        fault_inject="",
    )
    return Settings(**(base | kw))


def run(llm, question="q", **kw):
    events = []
    inp = RunInput(question, **{k: kw.pop(k) for k in ("clarification", "fault_inject") if k in kw})
    result = run_agent(inp, llm, settings(**kw), lambda t, p: events.append((t, p)))
    return result, events


def test_weak_result_triggers_rephrase_then_grounded_answer():
    llm = ScriptedLLM(
        [
            tool_turn(("search_documents", {"query": "bad wording", "purpose": "sleep hours"})),
            tool_turn(("search_documents", {"query": "adult sleep hours per night"})),
            text_turn("Adults need at least 7-8 hours of sleep each night [sleep:8]."),
        ]
    )
    result, events = run(llm)
    statuses = [p["status"] for t, p in events if t == "tool_result"]
    assert statuses == ["weak", "ok"]  # the result-driven change of action
    assert result.status == "answered" and result.steps == 3 and result.tool_calls == 2
    assert result.citation_report["all_supported"]
    # the weak status and its rephrase hint reached the model
    second_call_history = llm.calls[1]["messages"][-1]["content"][0]["content"]
    assert "status: weak" in second_call_history and "Rephrase" in second_call_history


def test_parallel_subquestions_combine_evidence_from_two_documents():
    llm = ScriptedLLM(
        [
            tool_turn(
                ("search_documents", {"query": "sleep hours"}),
                ("search_documents", {"query": "weekly activity minutes"}),
            ),
            text_turn(
                "Adults need at least 7-8 hours of sleep [sleep:8]. "
                "Adults should do at least 150 minutes a week of moderate activity [pag-2018:56]."
            ),
        ]
    )
    result, _ = run(llm)
    keys = {c["key"] for c in result.citation_report["checks"]}
    assert keys == {"sleep:8", "pag-2018:56"} and result.citation_report["all_supported"]


def test_clarification_is_terminal_and_returned():
    llm = ScriptedLLM(
        [
            tool_turn(
                ("ask_clarification", {"question": "Which edition?", "options": ["2015", "2020"]})
            )
        ]
    )
    result, events = run(llm)
    assert result.status == "clarification" and result.clarification["options"] == ["2015", "2020"]
    assert len(llm.calls) == 1 and ("clarification", result.clarification) in events


def test_followup_message_carries_original_question():
    llm = ScriptedLLM([text_turn("ok")])
    pending = {
        "original_question": "What do the guidelines say about sodium?",
        "question": "Which edition?",
        "options": ["2015-2020", "2020-2025"],
    }
    run(llm, question="2020-2025", clarification=pending)
    first_user = llm.calls[0]["messages"][0]["content"]
    assert "Original question: What do the guidelines say about sodium?" in first_user
    assert "User's reply: 2020-2025" in first_user


def test_step_limit_produces_partial_answer_with_tools_disabled():
    llm = ScriptedLLM(
        [
            tool_turn(("search_documents", {"query": "sleep"})),
            tool_turn(("search_documents", {"query": "activity"})),
            text_turn("Partial: adults need 7-8 hours [sleep:8].\nNot completed: activity part."),
        ]
    )
    result, events = run(llm, max_steps=2)
    assert result.status == "partial" and result.stop_reason == "step_limit"
    assert llm.calls[-1]["allow_tools"] is False
    assert "Not completed" in result.answer
    assert any(t == "limit_reached" for t, _ in events)


def test_token_budget_stops_the_run():
    llm = ScriptedLLM([tool_turn(("search_documents", {"query": "sleep"})), text_turn("partial")])
    result, _ = run(llm, max_tokens_per_run=500)  # first turn alone uses 1,100 tokens
    assert result.stop_reason == "token_budget" and result.status == "partial"


def test_tool_timeout_is_retried_and_recovers():
    llm = ScriptedLLM(
        [
            tool_turn(("search_documents", {"query": "sleep"})),
            text_turn("Adults need at least 7-8 hours of sleep [sleep:8]."),
        ]
    )
    result, events = run(llm, tool_timeout_s=0.3, fault_inject="search_documents:timeout:1")
    tool_result = next(p for t, p in events if t == "tool_result")
    assert tool_result["status"] == "ok" and tool_result["attempts"] == 2
    assert result.status == "answered"


def test_persistent_tool_failure_is_reported_to_model_as_error():
    llm = ScriptedLLM(
        [
            tool_turn(("search_documents", {"query": "sleep"})),
            text_turn("The search tool failed, so this could not be checked."),
        ]
    )
    result, events = run(llm, fault_inject="search_documents:error:9")
    tool_result = next(p for t, p in events if t == "tool_result")
    assert tool_result["status"] == "error" and tool_result["attempts"] == 3
    block = llm.calls[1]["messages"][-1]["content"][0]
    assert block["is_error"] is True


def test_llm_failure_returns_evidence_only_partial():
    llm = ScriptedLLM(
        [tool_turn(("search_documents", {"query": "sleep"})), LLMError("APIConnectionError: boom")]
    )
    result, _ = run(llm)
    assert result.status == "partial" and result.stop_reason == "llm_error"
    assert "[sleep:8]" in result.answer and "Not completed" in result.answer


def test_fabricated_citation_is_flagged():
    llm = ScriptedLLM(
        [
            tool_turn(("search_documents", {"query": "sleep"})),
            text_turn("Adults need 7-8 hours [sleep:8]. Teens need 12 hours [sleep:30]."),
        ]
    )
    result, _ = run(llm)
    by_key = {c["key"]: c["status"] for c in result.citation_report["checks"]}
    assert by_key == {"sleep:8": "supported", "sleep:30": "not_retrieved"}
    assert not result.citation_report["all_supported"]


def test_stub_llm_runs_full_loop():
    result, events = run(StubLLM(), question="How much sleep do adults need?")
    assert result.status == "answered" and "[sleep:8]" in result.answer
    assert [t for t, _ in events][:3] == ["run_started", "llm_call", "tool_call"]


# --- baseline (single pass)

from app.baseline.run import run_baseline  # noqa: E402


def run_base(llm, question="How much sleep do adults need?", **kw):
    events = []
    fault = kw.pop("fault_inject", None)
    result = run_baseline(
        RunInput(question, fault_inject=fault),
        llm,
        settings(**kw),
        lambda t, p: events.append((t, p)),
    )
    return result, events


def test_baseline_is_exactly_one_search_and_one_generation():
    llm = ScriptedLLM([text_turn("Adults need at least 7-8 hours of sleep [sleep:8].")])
    result, events = run_base(llm)
    assert [t for t, _ in events] == [
        "run_started",
        "tool_call",
        "tool_result",
        "llm_call",
        "run_finished",
    ]
    assert len(llm.calls) == 1 and llm.calls[0]["allow_tools"] is False
    assert 'source="sleep:8"' in llm.calls[0]["messages"][0]["content"]
    assert result.steps == 1 and result.citation_report["all_supported"]


def test_baseline_cannot_recover_from_weak_retrieval():
    llm = ScriptedLLM([text_turn("The documents do not say.")])
    _, events = run_base(llm, question="bad wording about sleep")
    assert next(p for t, p in events if t == "tool_result")["status"] == "weak"
    assert len(llm.calls) == 1  # no second search: that's what the agent adds


def test_baseline_with_failing_search_still_answers_gracefully():
    llm = ScriptedLLM([text_turn("Search failed; no evidence available.")])
    result, events = run_base(llm, fault_inject="search_documents:error:9")
    assert next(p for t, p in events if t == "tool_result")["status"] == "error"
    assert result.status == "answered" and result.evidence == {}
