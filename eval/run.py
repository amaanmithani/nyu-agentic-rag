"""Repeatable evaluation: agent vs single-pass baseline on the same corpus, model and API.

    uv run --project api python -m eval.run --mode both          # full run (~$1)
    uv run --project api python -m eval.run --mode agent --cases 4-ambiguous-sleep
    uv run --project api python -m eval.run --no-judge             # rules only, no judge cost

Every case runs through the public HTTP API (POST /sessions, POST /sessions/{id}/ask),
in a fresh session per case and mode, so the eval exercises exactly what the UI does.
Writes eval/results/<timestamp>.json (full traces) and eval/results/<timestamp>.md.
"""

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

import anthropic
import httpx
import yaml

from eval.score import JUDGE_MODEL, judge_answer, score_answer

ROOT = Path(__file__).resolve().parent.parent
CASES = Path(__file__).parent / "cases.yaml"
RESULTS = Path(__file__).parent / "results"
JUDGE_PRICE = {"in": 0.10, "out": 0.50}  # claude-haiku-5-5, USD per MTok


def load_env() -> None:
    """Read ANTHROPIC_API_KEY (for the judge) from .env without printing it."""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def ask(http: httpx.Client, sid: str, question: str, mode: str, fault: str | None) -> dict:
    body = {"question": question, "mode": mode, "fault_inject": fault}
    r = http.post(f"/sessions/{sid}/ask", params={"stream": "false"}, json=body)
    r.raise_for_status()
    data = r.json()
    finished = next(e["data"] for e in data["events"] if e["type"] == "run_finished")
    return {"run_id": data["run_id"], "events": data["events"], **finished}


def run_case(http, judge_client, case: dict, mode: str) -> dict:
    sid = http.post("/sessions").json()["session_id"]
    turns = [(case["question"], case, case.get("fault_inject"))]
    if case.get("followup"):
        turns.append((case["followup"]["question"], case["followup"], None))

    out = {
        "case": case["id"],
        "category": case["category"],
        "mode": mode,
        "session_id": sid,
        "turns": [],
    }
    for question, spec, fault in turns:
        run = ask(http, sid, question, mode, fault)
        verdict, judge_usage = (None, None)
        if judge_client:
            verdict, judge_usage = judge_answer(judge_client, question, spec, run["answer"])
        scored = score_answer(spec, run, verdict)
        out["turns"].append(
            {
                "question": question,
                "expected": spec["expected_behavior"],
                "run": run,
                "score": scored,
                "judge_usage": judge_usage,
            }
        )
        print(
            f"  [{mode:8}] {case['id']:28} {'PASS' if scored['pass'] else 'FAIL'}  "
            f"{run['status']:13} tools={run['tool_calls']} {run['latency_ms'] / 1000:5.1f}s "
            f"${run['cost_usd']:.4f}  {'; '.join(scored['notes'])[:110]}"
        )
    out["pass"] = all(t["score"]["pass"] for t in out["turns"])
    return out


def summarize(results: list[dict], mode: str) -> dict:
    rs = [r for r in results if r["mode"] == mode]
    turns = [t for r in rs for t in r["turns"]]
    runs = [t["run"] for t in turns]
    support = [
        t["score"]["citation_support"] for t in turns if t["score"]["citation_support"] is not None
    ]
    judged = [t["score"]["checks"]["judge"] for t in turns if "judge" in t["score"]["checks"]]
    n = len(rs) or 1
    return {
        "cases": len(rs),
        "passed": sum(r["pass"] for r in rs),
        "correct_judge": f"{sum(judged)}/{len(judged)}" if judged else "n/a",
        "citation_support": round(sum(support) / len(support), 3) if support else None,
        "fabricated_citations": sum(r["citations"].get("not_retrieved", 0) for r in runs),
        "completed": sum(r["status"] in ("answered", "clarification") for r in runs),
        "turns": len(runs),
        "avg_tool_calls": round(sum(r["tool_calls"] for r in runs) / len(runs), 2),
        "avg_latency_s": round(sum(r["latency_ms"] for r in runs) / len(runs) / 1000, 2),
        "avg_tokens": int(sum(r["tokens"]["total"] for r in runs) / len(runs)),
        "total_cost_usd": round(sum(r["cost_usd"] for r in runs), 4),
        "cost_per_case_usd": round(sum(r["cost_usd"] for r in runs) / n, 4),
    }


def write_report(path: Path, results: list[dict], summaries: dict, meta: dict) -> None:
    L = [
        f"# Eval run {meta['timestamp']}",
        "",
        f"Model `{meta['model']}` · judge `{meta['judge']}` · API `{meta['api']}` · "
        f"{meta['corpus_docs']} documents",
        "",
        "## Summary",
        "",
        "| metric | " + " | ".join(summaries) + " |",
        "|---|" + "---|" * len(summaries),
    ]
    for key in next(iter(summaries.values())):
        L.append(f"| {key} | " + " | ".join(str(s[key]) for s in summaries.values()) + " |")
    L += [
        "",
        f"Judge cost: ${meta['judge_cost_usd']:.4f}",
        "",
        "## Per case",
        "",
        "| case | mode | pass | status | tools | latency s | tokens | cost $ | "
        "cit. support | notes |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        for i, t in enumerate(r["turns"]):
            run, s = t["run"], t["score"]
            name = r["case"] + (" (follow-up)" if i else "")
            sup = "-" if s["citation_support"] is None else f"{s['citation_support']:.0%}"
            notes = "; ".join(s["notes"]).replace("|", "/") or ""
            L.append(
                f"| {name} | {r['mode']} | {'✅' if s['pass'] else '❌'} | {run['status']} | "
                f"{run['tool_calls']} | {run['latency_ms'] / 1000:.1f} | "
                f"{run['tokens']['total']} | {run['cost_usd']:.4f} | {sup} | {notes} |"
            )
    L += ["", "## Answers and traces", ""]
    for r in results:
        for i, t in enumerate(r["turns"]):
            L.append(f"### {r['case']} · {r['mode']}{' · follow-up' if i else ''}")
            L.append(f"**Q:** {t['question']}  ")
            L.append(f"**Cited:** {', '.join(t['score']['cited']) or 'none'}  ")
            for e in t["run"]["events"]:
                d = e["data"]
                if e["type"] == "tool_call":
                    L.append(f"- `{d['tool']}` {json.dumps(d['args'])}")
                elif e["type"] == "tool_result":
                    L.append(
                        f"  - → **{d['status']}** ({d['ms']} ms, {d['attempts']} attempt(s))"
                        f" {d['summary'][:140]}"
                    )
                elif e["type"] in ("limit_reached", "clarification", "llm_error"):
                    L.append(f"- _{e['type']}_: {json.dumps(d)[:200]}")
            L += ["", "> " + t["run"]["answer"].replace("\n", "\n> "), ""]
    path.write_text("\n".join(L))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["agent", "baseline", "both"], default="both")
    ap.add_argument("--cases", nargs="*", help="case ids to run (default: all)")
    ap.add_argument("--api", default=os.environ.get("EVAL_API", "http://localhost:8000"))
    ap.add_argument("--no-judge", action="store_true")
    args = ap.parse_args()

    load_env()
    cases = yaml.safe_load(CASES.read_text())["cases"]
    if args.cases:
        cases = [c for c in cases if c["id"] in args.cases]
    modes = ["agent", "baseline"] if args.mode == "both" else [args.mode]
    judge = None if args.no_judge else anthropic.Anthropic()

    http = httpx.Client(base_url=args.api, timeout=180)
    health = http.get("/health").json()
    if health.get("models") != "ready":
        raise SystemExit(f"API not ready: {health}")
    docs = http.get("/documents").json()["documents"]

    t0 = time.monotonic()
    results = []
    for case in cases:
        for mode in modes:
            results.append(run_case(http, judge, case, mode))

    judge_cost = sum(
        (
            t["judge_usage"]["input"] * JUDGE_PRICE["in"]
            + t["judge_usage"]["output"] * JUDGE_PRICE["out"]
        )
        / 1e6
        for r in results
        for t in r["turns"]
        if t["judge_usage"]
    )
    summaries = {m: summarize(results, m) for m in modes}
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    first_run = results[0]["turns"][0]["run"] if results else {}
    meta = {
        "timestamp": ts,
        "model": first_run.get("model"),
        "judge": None if args.no_judge else JUDGE_MODEL,
        "api": args.api,
        "corpus_docs": len(docs),
        "judge_cost_usd": round(judge_cost, 4),
        "wall_clock_s": round(time.monotonic() - t0, 1),
    }

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{ts}.json").write_text(
        json.dumps({"meta": meta, "summary": summaries, "results": results}, indent=2, default=str)
    )
    write_report(RESULTS / f"{ts}.md", results, summaries, meta)

    print("\n" + json.dumps(summaries, indent=2))
    print(f"judge cost ${judge_cost:.4f} · wall clock {meta['wall_clock_s']}s")
    print(f"wrote eval/results/{ts}.json and .md")


if __name__ == "__main__":
    main()
