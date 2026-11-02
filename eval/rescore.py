"""Re-score a saved eval run with the current scoring rules, without calling any API.

    uv run --project api python -m eval.rescore eval/results/<timestamp>.json

Uses the saved answers, citation reports and judge verdicts; only the rule-based checks
(eval/score.py) and the case specs (eval/cases.yaml) are re-applied. Writes
<timestamp>.rescored.json/.md next to the original, which is left untouched.
"""

import json
import sys
from pathlib import Path

import yaml

from eval.run import CASES, summarize, write_report
from eval.score import score_answer


def main() -> None:
    src = Path(sys.argv[1])
    data = json.loads(src.read_text())
    specs = {c["id"]: c for c in yaml.safe_load(CASES.read_text())["cases"]}
    for r in data["results"]:
        case = specs[r["case"]]
        turn_specs = [case] + ([case["followup"]] if case.get("followup") else [])
        for t, spec in zip(r["turns"], turn_specs, strict=True):
            t["score"] = score_answer(spec, t["run"], t["score"].get("judge"))
        r["pass"] = all(t["score"]["pass"] for t in r["turns"])
    modes = list(data["summary"])
    data["summary"] = {m: summarize(data["results"], m) for m in modes}
    data["meta"]["rescored_from"] = src.name
    out = src.with_suffix(".rescored.json")
    out.write_text(json.dumps(data, indent=2, default=str))
    write_report(src.with_suffix(".rescored.md"), data["results"], data["summary"], data["meta"])
    for m in modes:
        s = data["summary"][m]
        print(f"{m:8} passed {s['passed']}/{s['cases']}  judge {s['correct_judge']}")
    print(f"wrote {out.name} and .md")


if __name__ == "__main__":
    main()
