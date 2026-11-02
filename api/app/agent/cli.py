"""Run one question through the agent from the command line and print the live trace.

docker compose exec api python -m app.agent.cli "How much sleep do adults need?"
docker compose exec api python -m app.agent.cli --fault search_documents:timeout:1 "..."
"""

import argparse
import json

from app.agent.llm import make_llm
from app.agent.loop import RunInput, run_agent
from app.config import get_settings


def print_event(kind: str, p: dict) -> None:
    if kind == "llm_call":
        t = p["tokens"]
        print(
            f"  [llm] step {p['step']}  {p['ms']} ms  in={t['input']} cache_read="
            f"{t['cache_read']} out={t['output']}  -> {p['tool_calls'] or p['stop_reason']}"
        )
    elif kind == "tool_call":
        print(f"  [call] {p['tool']} {json.dumps(p['args'])}")
    elif kind == "tool_result":
        flags = f"  INJECTION FLAGGED: {p['injection_flags']}" if p["injection_flags"] else ""
        print(
            f"  [result] {p['status']} ({p['ms']} ms, attempts={p['attempts']}) "
            f"{p['summary'][:160]}{flags}"
        )
    elif kind in ("limit_reached", "llm_error", "clarification"):
        print(f"  [{kind}] {p}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--fault", default=None, help='e.g. "search_documents:timeout:1"')
    args = ap.parse_args()

    result = run_agent(
        RunInput(args.question, fault_inject=args.fault), make_llm(), get_settings(), print_event
    )
    print(f"\n=== {result.status} (stop: {result.stop_reason}) ===\n{result.answer}\n")
    r = result.citation_report
    print(
        f"citations: {r['supported']} supported, {r['unsupported']} unsupported, "
        f"{r['not_retrieved']} not retrieved"
    )
    for c in r["checks"]:
        if c["status"] != "supported":
            print(f"  ! {c['key']} {c['status']}: {c['reason']}")
    print(
        f"steps={result.steps} tool_calls={result.tool_calls} latency={result.latency_ms} ms "
        f"tokens={result.usage.total} cost=${result.cost_usd:.4f} model={result.model}"
    )


if __name__ == "__main__":
    main()
