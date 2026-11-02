"""Re-score a saved eval run with the current scoring rules, without calling any API.

    uv run --project api python -m eval.rescore eval/results/<timestamp>.json
    uv run --project api python -m eval.rescore <file> --citation-llm   # + semantic citation
                                                                         # check (Haiku, ~$0.03)

Uses the saved answers, citation reports and judge verdicts; only the rule-based checks
(eval/score.py) and the case specs (eval/cases.yaml) are re-applied. Writes
<timestamp>.rescored.json/.md next to the original, which is left untouched.
"""

import argparse
import json
import os
from pathlib import Path

import anthropic
import httpx
import yaml

from eval.citation_check import check_turn, parse_verdict
from eval.run import CASES, PAGE_CACHE, load_env, summarize, write_report
from eval.score import score_answer


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--citation-llm", action="store_true", help="add the semantic citation check")
    ap.add_argument("--api", default=os.environ.get("EVAL_API", "http://localhost:8000"))
    args = ap.parse_args()
    src = Path(args.results)
    data = json.loads(src.read_text())
    client = http = None
    if args.citation_llm:
        load_env()
        client, http = anthropic.Anthropic(), httpx.Client(base_url=args.api, timeout=60)
    specs = {c["id"]: c for c in yaml.safe_load(CASES.read_text())["cases"]}
    for r in data["results"]:
        case = specs[r["case"]]
        turn_specs = [case] + ([case["followup"]] if case.get("followup") else [])
        for t, spec in zip(r["turns"], turn_specs, strict=True):
            t["score"] = score_answer(spec, t["run"], t["score"].get("judge"))
            if client and not t.get("citation_llm"):
                t["citation_llm"] = check_turn(client, http, t["run"], PAGE_CACHE)
            for c in (t.get("citation_llm") or {}).get("checks", []):
                if c["verdict"] == "unparseable":  # repair from the stored raw text
                    c["verdict"] = parse_verdict(c["reason"]).get("verdict", "unparseable")
        r["pass"] = all(t["score"]["pass"] for t in r["turns"])
    modes = list(data["summary"])
    data["summary"] = {m: summarize(data["results"], m) for m in modes}
    stem = src.name.removesuffix(".json").removesuffix(".rescored")
    out = src.with_name(f"{stem}.rescored.json")  # re-scoring a re-scored file updates it
    data["meta"]["rescored_from"] = f"{stem}.json"
    out.write_text(json.dumps(data, indent=2, default=str))
    write_report(out.with_suffix(".md"), data["results"], data["summary"], data["meta"])
    for m in modes:
        s = data["summary"][m]
        print(
            f"{m:8} passed {s['passed']}/{s['cases']}  judge {s['correct_judge']}  "
            f"citations semantic {s['citation_support_semantic']} "
            f"(unsupported {s['citations_unsupported_semantic']})"
        )
    print(f"wrote {out.name} and .md")


if __name__ == "__main__":
    main()
