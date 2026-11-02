"""Tool contract tests. The DB/search layer is faked, so these run without Postgres."""

import time

import pytest

from app.tools import corpus, definitions
from app.tools.executor import FaultPlan, execute

DOCS = [
    {"id": "pag-2018", "title": "Physical Activity Guidelines 2018", "page_count": 118},
    {"id": "sleep", "title": "Healthy Sleep", "page_count": 72},
]


def hit(doc_id: str, page: int, score: float) -> dict:
    return {"doc_id": doc_id, "page": page, "rerank_score": score, "text": f"text {page}"}


@pytest.fixture
def fake_corpus(monkeypatch):
    monkeypatch.setattr(corpus, "list_ready_documents", lambda: DOCS)
    monkeypatch.setattr(corpus, "ready_doc_ids", lambda: {d["id"] for d in DOCS})
    monkeypatch.setattr(
        corpus, "get_document", lambda doc_id: next((d for d in DOCS if d["id"] == doc_id), None)
    )
    monkeypatch.setattr(
        corpus,
        "get_pages",
        lambda doc_id, s, e: [{"page": p, "text": f"page {p} text"} for p in range(s, e + 1)],
    )


def set_hits(monkeypatch, hits: list[dict]):
    monkeypatch.setattr(
        definitions.retrieval, "hybrid_search", lambda q, d, k: {"hits": hits, "cached": False}
    )


def run(name: str, args: dict, **kw):
    return execute(
        name, args, timeout_s=kw.pop("timeout_s", 2.0), max_retries=kw.pop("max_retries", 2), **kw
    )


# --- list_documents


def test_list_documents_ok(fake_corpus):
    r = run("list_documents", {})
    assert r.status == "ok" and len(r.data) == 2


def test_list_documents_empty(monkeypatch):
    monkeypatch.setattr(corpus, "list_ready_documents", lambda: [])
    assert run("list_documents", {}).status == "empty"


# --- search_documents


def test_search_ok_returns_citations(fake_corpus, monkeypatch):
    set_hits(monkeypatch, [hit("sleep", 8, 7.2), hit("sleep", 8, 5.0), hit("sleep", 25, 5.7)])
    r = run("search_documents", {"query": "hours of sleep for adults"})
    assert r.status == "ok"
    assert [c.key() for c in r.citations] == ["sleep:8", "sleep:25"]  # deduplicated


def test_search_weak_when_best_score_below_threshold(fake_corpus, monkeypatch):
    set_hits(monkeypatch, [hit("pag-2018", 61, -8.0)])
    r = run("search_documents", {"query": "creatine dosage"})
    assert r.status == "weak"
    assert "Rephrase" in r.note and r.data  # passages still returned for inspection


def test_search_empty(fake_corpus, monkeypatch):
    set_hits(monkeypatch, [])
    assert run("search_documents", {"query": "anything"}).status == "empty"


def test_search_unknown_doc_filter_is_error(fake_corpus, monkeypatch):
    set_hits(monkeypatch, [hit("sleep", 1, 3.0)])
    r = run("search_documents", {"query": "sleep", "doc_ids": ["nope"]})
    assert r.status == "error" and "nope" in r.note


@pytest.mark.parametrize(
    "args",
    [{}, {"query": " "}, {"query": "x" * 301}, {"query": "ok query", "top_k": 50}],
)
def test_search_invalid_arguments(fake_corpus, args):
    r = run("search_documents", args)
    assert r.status == "error" and r.note.startswith("invalid arguments")
    assert r.attempts == 0  # validation errors are never retried


# --- read_document_section


def test_read_section_ok(fake_corpus):
    r = run("read_document_section", {"doc_id": "sleep", "page_start": 8, "page_end": 9})
    assert r.status == "ok"
    assert [c.key() for c in r.citations] == ["sleep:8", "sleep:9"]


def test_read_section_defaults_to_single_page(fake_corpus):
    r = run("read_document_section", {"doc_id": "sleep", "page_start": 8})
    assert [d["page"] for d in r.data] == [8]


def test_read_section_unknown_doc(fake_corpus):
    r = run("read_document_section", {"doc_id": "missing", "page_start": 1})
    assert r.status == "error" and "unknown doc_id" in r.note


def test_read_section_page_out_of_range(fake_corpus):
    r = run("read_document_section", {"doc_id": "sleep", "page_start": 72, "page_end": 74})
    assert r.status == "error" and "72 pages" in r.note


@pytest.mark.parametrize(
    "args",
    [
        {"doc_id": "sleep", "page_start": 0},
        {"doc_id": "sleep", "page_start": 5, "page_end": 4},
        {"doc_id": "sleep", "page_start": 1, "page_end": 4},  # 4 pages > max 3
    ],
)
def test_read_section_invalid_range(fake_corpus, args):
    assert run("read_document_section", args).status == "error"


def test_read_section_blank_pages_are_empty(fake_corpus, monkeypatch):
    monkeypatch.setattr(corpus, "get_pages", lambda d, s, e: [{"page": s, "text": "  "}])
    assert run("read_document_section", {"doc_id": "sleep", "page_start": 1}).status == "empty"


# --- ask_clarification


def test_clarification_is_terminal():
    r = run("ask_clarification", {"question": "Which edition?", "options": ["2015", "2020"]})
    assert r.status == "ok" and r.terminal
    assert r.data["options"] == ["2015", "2020"]


def test_clarification_needs_two_options():
    assert run("ask_clarification", {"question": "Which one?", "options": ["a"]}).status == "error"


# --- executor: unknown tool, crashes, timeouts, retries, faults


def test_unknown_tool():
    r = run("delete_everything", {})
    assert r.status == "error" and "unknown tool" in r.note


def test_tool_crash_is_reported_not_raised(fake_corpus, monkeypatch):
    def boom(*a):
        raise KeyError("bug")

    monkeypatch.setattr(definitions.retrieval, "hybrid_search", boom)
    r = run("search_documents", {"query": "sleep"})
    assert r.status == "error" and "KeyError" in r.note and r.attempts == 1


def test_injected_timeout_is_retried_then_recovers(fake_corpus, monkeypatch):
    set_hits(monkeypatch, [hit("sleep", 8, 7.0)])
    faults = FaultPlan("search_documents:timeout:1")
    r = run("search_documents", {"query": "sleep"}, timeout_s=0.3, faults=faults)
    assert r.status == "ok" and r.attempts == 2


def test_persistent_fault_stops_after_bounded_retries(fake_corpus, monkeypatch):
    set_hits(monkeypatch, [hit("sleep", 8, 7.0)])
    faults = FaultPlan("search_documents:error:5")
    r = run("search_documents", {"query": "sleep"}, max_retries=2, faults=faults)
    assert r.status == "error" and r.attempts == 3  # 1 try + 2 retries, then give up
    assert "failed after 3 attempt" in r.note


def test_all_attempts_time_out(fake_corpus, monkeypatch):
    set_hits(monkeypatch, [hit("sleep", 8, 7.0)])
    faults = FaultPlan("search_documents:timeout:9")
    r = run("search_documents", {"query": "sleep"}, timeout_s=0.2, max_retries=1, faults=faults)
    assert r.status == "timeout" and r.attempts == 2


def test_run_deadline_caps_tool_time(fake_corpus, monkeypatch):
    set_hits(monkeypatch, [hit("sleep", 8, 7.0)])
    faults = FaultPlan("search_documents:timeout:9")
    t0 = time.monotonic()
    r = run(
        "search_documents",
        {"query": "sleep"},
        timeout_s=5.0,
        faults=faults,
        deadline=time.monotonic() + 0.4,
    )
    assert r.status == "timeout"
    assert time.monotonic() - t0 < 1.5  # did not wait the full 5s timeout x retries


def test_fault_plan_parsing():
    plan = FaultPlan("search_documents:timeout:1, read_document_section:error:2")
    assert plan.take("search_documents") == "timeout"
    assert plan.take("search_documents") is None
    assert plan.take("read_document_section") == "error"
    with pytest.raises(ValueError):
        FaultPlan("search_documents:explode:1")


def test_anthropic_schemas_are_well_formed():
    for tool in definitions.TOOLS.values():
        s = tool.anthropic_schema()
        assert s["name"] == tool.name and s["input_schema"]["type"] == "object"
        assert len(s["description"]) > 40
