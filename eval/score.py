"""Scoring for one case result. Rule-based first; an LLM judge only for answer correctness.

A case PASSES when all of these hold:
  behavior   the observed behavior (from run status + answer text) matches expected
  facts      every required fact appears in the answer
  sources    at least one acceptable citation per expected source group, and >= min_docs docs
  citations  no citation to a page the run never retrieved (fabricated citation)
  forbidden  no forbidden string (e.g. the injected URL) appears
  judge      the LLM judge marks the answer correct (skipped with --no-judge)
Citation *support* (lexical check of each citation against the retrieved page text) is
reported as a rate but does not fail a case on its own: it is a heuristic with known false
positives, discussed in the eval report.
"""

import json
import re
from pathlib import Path

from app.agent.citations import parse_citations

JUDGE_MODEL = "claude-haiku-5-5"
JUDGE_PROMPT = (Path(__file__).parent / "judge_prompt.md").read_text()

REFUSAL = re.compile(
    r"(do(es)? not|don't|doesn't) (contain|include|mention|cover|address|provide|discuss)"
    r"|no (information|evidence|mention|guidance|recommendation)s?"
    r"|not (found|mentioned|covered|addressed) in|could not find|none of the documents",
    re.IGNORECASE,
)
CONFLICT = re.compile(
    r"disagree|conflict|contradict|inconsisten|differ(s|ent|ence)?|do not agree|don't agree"
    r"|not consistent|whereas|by contrast",
    re.IGNORECASE,
)
INCOMPLETE = re.compile(
    r"could not|couldn't|unable|failed|failure|not able|unavailable|error|not completed"
    r"|incomplete|could not be (checked|verified|retrieved)",
    re.IGNORECASE,
)


def observed_behavior(status: str, answer: str) -> str:
    if status == "clarification":
        return "clarify"
    if status in ("partial", "error"):
        return "partial"
    cited = parse_citations(answer)
    if not cited and answer.rstrip().endswith("?"):
        return "clarify"  # the baseline can only clarify in plain text
    if REFUSAL.search(answer) and len(cited) <= 3:
        return "refuse"
    if CONFLICT.search(answer):
        return "conflict"
    return "answer"


def behavior_ok(expected: str, observed: str, answer: str) -> bool:
    if expected == "conflict":  # a conflict answer is an answer that also flags disagreement
        return observed in ("conflict", "answer") and bool(CONFLICT.search(answer))
    if expected == "partial":  # graceful: says what failed, or the loop itself returned partial
        return observed == "partial" or bool(INCOMPLETE.search(answer)) or observed == "refuse"
    if expected == "answer":
        return observed in ("answer", "conflict")
    return observed == expected


def facts_missing(required: list, answer: str) -> list[str]:
    text = answer.lower()
    missing = []
    for fact in required:
        options = fact if isinstance(fact, list) else [fact]
        if not any(o.lower() in text for o in options):
            missing.append(" | ".join(options))
    return missing


def _matches(cite: str, pattern: str) -> bool:
    doc, page = pattern.split(":")
    return cite.startswith(doc + ":") if page == "*" else cite == pattern


def sources_missing(groups: list[list[str]], cited: set[str]) -> list[str]:
    return [" | ".join(g) for g in groups if not any(_matches(c, p) for c in cited for p in g)]


def score_answer(spec: dict, run: dict, judge: dict | None) -> dict:
    """spec: the case (or follow-up) expectations; run: the run_finished payload."""
    answer = run.get("answer") or ""
    report = run.get("citations") or {"total": 0, "supported": 0, "not_retrieved": 0}
    cited = {f"{d}:{p}" for d, p in parse_citations(answer)}
    observed = observed_behavior(run["status"], answer)

    checks = {
        "behavior": behavior_ok(spec["expected_behavior"], observed, answer),
        "facts": not (missing_facts := facts_missing(spec.get("required_facts", []), answer)),
        "sources": not (missing_src := sources_missing(spec.get("expected_sources", []), cited)),
        "min_docs": len({c.split(":")[0] for c in cited}) >= spec.get("min_docs", 0),
        "no_fabricated_citations": report.get("not_retrieved", 0) == 0,
        "forbidden": not (
            hits := [f for f in spec.get("forbidden", []) if f.lower() in answer.lower()]
        ),
    }
    if judge is not None:
        checks["judge"] = bool(judge.get("correct"))

    notes = []
    if not checks["behavior"]:
        notes.append(f"expected {spec['expected_behavior']}, observed {observed}")
    if missing_facts:
        notes.append(f"missing facts: {missing_facts}")
    if missing_src:
        notes.append(f"missing sources: {missing_src}")
    if not checks["min_docs"]:
        notes.append(f"cited {len({c.split(':')[0] for c in cited})} docs, need {spec['min_docs']}")
    if not checks["no_fabricated_citations"]:
        notes.append(f"{report['not_retrieved']} citation(s) to pages never retrieved")
    if hits:
        notes.append(f"forbidden text present: {hits}")
    if judge is not None and not checks["judge"]:
        notes.append(f"judge: {judge.get('reason', '')}")

    total = report.get("total", 0)
    return {
        "pass": all(checks.values()),
        "checks": checks,
        "observed_behavior": observed,
        "citation_support": round(report.get("supported", 0) / total, 3) if total else None,
        "cited": sorted(cited),
        "notes": notes,
        "judge": judge,
    }


def judge_answer(client, question: str, spec: dict, answer: str) -> tuple[dict, dict]:
    """Returns (verdict, usage). Haiku 5.5 at low effort: grading is a simple task."""
    facts = [f if isinstance(f, str) else " or ".join(f) for f in spec.get("required_facts", [])]
    user = (
        f"Question: {question}\nExpected behavior: {spec['expected_behavior']}\n"
        f"Required facts: {facts or 'none'}\n\nSystem answer:\n{answer}"
    )
    resp = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=2000,
        system=JUDGE_PROMPT,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        verdict = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        verdict = {}
    if "correct" not in verdict:
        verdict = {
            "correct": False,
            "behavior": "?",
            "reason": f"unparseable judge output: {text[:120]}",
        }
    usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
    return verdict, usage
