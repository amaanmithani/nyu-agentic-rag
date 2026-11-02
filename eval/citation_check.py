"""Semantic citation verification: does each cited page actually support the claim citing it?

The runtime check (api/app/agent/citations.py) is fast and free but lexical: it confirms the
page was retrieved and that the claim's numbers and enough of its words are on the page. This
eval-time check reads the claim and the cited page's full text (GET /documents/{id}/pages/{p})
and asks a small model whether the page supports the claim. Same prompt for agent and baseline.
"""

import json
import re
from pathlib import Path

import httpx

CITATION_MODEL = "claude-haiku-5-5"
PROMPT = (Path(__file__).parent / "citation_judge_prompt.md").read_text()
MAX_PER_TURN = 20  # cost guard; a long answer rarely has more distinct citations than this
MAX_PAGE_CHARS = 8000


def parse_verdict(text: str) -> dict:
    """JSON verdict; if the JSON is malformed or truncated (seen in practice: unescaped quotes
    inside "reason"), fall back to reading the verdict field alone."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    field = re.search(r'"verdict"\s*:\s*"(supported|partial|unsupported)"', text)
    return {"verdict": field.group(1), "reason": text[:160]} if field else {}


def _page_text(http: httpx.Client, key: str, cache: dict) -> str | None:
    if key not in cache:
        doc, page = key.rsplit(":", 1)
        r = http.get(f"/documents/{doc}/pages/{page}")
        cache[key] = r.json()["text"][:MAX_PAGE_CHARS] if r.status_code == 200 else None
    return cache[key]


def check_turn(client, http: httpx.Client, run: dict, page_cache: dict) -> dict:
    """Returns {"checks": [...], "usage": {...}} for one answered turn."""
    checks, usage = [], {"input": 0, "output": 0}
    pairs = {(c["key"], c["sentence"]) for c in (run.get("citations") or {}).get("checks", [])}
    for key, sentence in sorted(pairs)[:MAX_PER_TURN]:
        page = _page_text(http, key, page_cache)
        if page is None:
            checks.append(
                {
                    "key": key,
                    "verdict": "unsupported",
                    "reason": "page not found",
                    "sentence": sentence,
                }
            )
            continue
        resp = client.messages.create(
            model=CITATION_MODEL,
            max_tokens=1000,
            system=PROMPT,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": f"CLAIM:\n{sentence}\n\nPAGE ({key}):\n{page}"}],
        )
        usage["input"] += resp.usage.input_tokens
        usage["output"] += resp.usage.output_tokens
        text = "".join(b.text for b in resp.content if b.type == "text")
        verdict = parse_verdict(text)
        checks.append(
            {
                "key": key,
                "sentence": sentence,
                "verdict": verdict.get("verdict", "unparseable"),
                "reason": verdict.get("reason", text[:120]),
            }
        )
    return {"checks": checks, "usage": usage}


def support_rate(checks: list[dict]) -> float | None:
    """supported = 1, partial = 0.5, unsupported = 0."""
    if not checks:
        return None
    weight = {"supported": 1.0, "partial": 0.5}
    return round(sum(weight.get(c["verdict"], 0.0) for c in checks) / len(checks), 3)
