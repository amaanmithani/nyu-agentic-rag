# AI Risk & Governance Assistant: an agentic RAG take-home

A full-stack **agentic RAG** application over a corpus of **AI risk and governance documents**
(NIST AI frameworks, U.S. federal OMB AI memoranda, the AI Bill of Rights, and a synthetic
university AI policy). Governance documents are **versioned, superseded and contradictory**: a
2025 memo rescinds a 2024 one, a deadline changes from 365 to 180 days, an internal policy
disagrees with NIST. A single retrieve-then-generate pass handles that poorly. Here a
hand-written Claude tool-use loop decomposes questions, rephrases weak searches, reads more
pages, asks for clarification when the answer depends on who is asking, and answers with
`[doc:page]` citations that are **verified against what the tools actually returned**.

Stack: React + Vite + TypeScript + Tailwind (nginx) · FastAPI (Python 3.12, uv) · PostgreSQL 16 +
pgvector · Redis 7 · Claude Sonnet 5.5 · local fastembed embeddings + cross-encoder reranker ·
Docker Compose · GitHub Actions · Kubernetes/OpenShift manifests.

| | Agent | Single-pass baseline |
|---|---|---|
| Eval cases passed (11 cases) | **10/11** | 5/11 |
| Judged correct (LLM judge, 12 turns) | **11/12** | 6/12 |
| Fabricated citations | 0 | 0 |
| Avg latency / cost per case | 9.2 s / $0.034 | 5.1 s / $0.020 |

Details and failure analysis: [docs/eval-report.md](docs/eval-report.md).

## Quickstart

Prerequisites: Docker Desktop (8 GB RAM for Docker), [uv](https://docs.astral.sh/uv/), an
Anthropic API key.

```bash
cp .env.example .env              # then set ANTHROPIC_API_KEY=... and a POSTGRES_PASSWORD
./corpus/download.sh              # 10 public PDFs, SHA256-verified, + the synthetic policy
docker compose up -d --build      # db, redis, api, web (first start downloads ~200 MB of models)
./scripts/seed.sh                 # uploads the corpus through the API (~2 min on CPU)
open http://localhost:8080
```

Try: *"Do we need a Chief AI Officer, and how soon?"*, *"Is M-24-10 still in effect?"*,
*"What do I have to do before deploying my AI system?"* (asks a clarifying question).
To demonstrate tool failure + bounded retry live, open **http://localhost:8080/?demo=failure**
(hidden demo mode; the server only accepts it when `ALLOW_FAULT_INJECTION=true`).

## Documentation

| Doc | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) · [docs/diagram.svg](docs/diagram.svg) | Components, retrieval, agent loop, decision points, stopping conditions, state, Redis keys, design trade-offs |
| [docs/eval-report.md](docs/eval-report.md) | 11 cases, agent vs baseline, per-case results, failure analysis, traces, exclusions |
| [docs/walkthrough.md](docs/walkthrough.md) | Reproducible demo: multi-doc, re-retrieval, weak→rephrase, clarification, failed tool call, injection, conflict |
| [docs/runbook.md](docs/runbook.md) | Deploy / verify / update / rollback, health output, end-to-end request, persistence, Redis outage, logging, scaling, CI, Kubernetes, OpenShift |
| [docs/summary.md](docs/summary.md) | What was built, what's unfinished, next steps, interview cheat sheet |
| [corpus/SOURCES.md](corpus/SOURCES.md) | Corpus sources, why this corpus, the synthetic document |

## Repository layout

```
api/app/
  main.py config.py db.py redis.py schema.sql   FastAPI app, settings, Postgres pool, Redis wrapper (circuit breaker)
  ingest/        pdf.py chunker.py embedder.py pipeline.py   parse -> chunk -> embed -> store
  retrieval.py   hybrid search: pgvector + full-text, RRF, cross-encoder rerank, Redis cache
  tools/         base.py (ToolResult) corpus.py (read-only SQL) definitions.py (4 tools) executor.py (timeout/retry/faults)
  agent/         loop.py prompts.py llm.py citations.py cli.py
  baseline/      run.py (retrieve once, generate once)
  sessions.py runs.py routes/{documents,chat}.py
api/tests/       75 unit tests (no API key, no database needed)
web/src/         App.tsx, components/{Chat,Answer,TracePanel,DocumentsPanel}.tsx, api/client.ts
eval/            cases.yaml run.py score.py rescore.py judge_prompt.md results/
corpus/          download.sh manifest.tsv SOURCES.md synthetic/ alternatives/health/
deploy/          k8s/{base,overlays/local,deploy.sh}  openshift/
scripts/         seed.sh smoke.sh edge_cases.sh
docker-compose.yml  .env.example  .github/workflows/ci.yml
```

## Reproducibility notes

- **Corpus:** `corpus/manifest.tsv` pins every source URL and its **SHA256**; `download.sh`
  refuses mismatched files. Sources and page numbering: [corpus/SOURCES.md](corpus/SOURCES.md).
- **Models:** agent and baseline `claude-sonnet-5-5` (adaptive thinking, effort `medium`,
  prompt caching); eval judge `claude-haiku-5-5` (effort `low`); embeddings
  `BAAI/bge-small-en-v1.5` (384-d); reranker `Xenova/ms-marco-MiniLM-L-6-v2`. All set in `.env`.
- **Prompts:** agent system prompt, wrap-up notice and tool-result formatting in
  [api/app/agent/prompts.py](api/app/agent/prompts.py); judge prompt in
  [eval/judge_prompt.md](eval/judge_prompt.md).
- **Tool schemas:** Pydantic models in [api/app/tools/definitions.py](api/app/tools/definitions.py)
  (sent to Claude as JSON Schema). Every tool returns
  `ToolResult{status: ok|empty|weak|error|timeout, data, note, citations}` and never raises.

  | Tool | Input | Output | Notable statuses |
  |---|---|---|---|
  | `list_documents` | none | id, title, page count per document | `empty` if no documents |
  | `search_documents` | `query` (2-300 chars), `doc_ids?` (≤10, must exist), `top_k` 1-10 (default 5), `purpose?` (sub-question, shown in trace) | passages with doc_id, page, rerank score, text | `weak` when best rerank score < 0.0, `empty`, `error` for unknown doc_ids |
  | `read_document_section` | `doc_id`, `page_start`, `page_end?` (max 3 pages) | full page text | `error` for unknown doc / out-of-range pages, `empty` for blank pages |
  | `ask_clarification` | `question`, `options` (2-5) | the question + options | terminal: the run stops and the session stores it |

- **Environment template:** [.env.example](.env.example) (no secrets).
- **Evaluation:** `uv run --project api python -m eval.run --mode both` (writes
  `eval/results/<timestamp>.json` + `.md`); re-score a saved run without API calls:
  `uv run --project api python -m eval.rescore eval/results/<timestamp>.json`.
- **Expected API cost:** ~$0.03 per agent question, ~$0.02 per baseline question; one full
  eval run (11 cases, both modes, judge) ≈ $0.60. The whole project, including development,
  three eval attempts and the domain switch, used ≈ $2.75.

## Tests

```bash
cd api && uv run pytest -q          # 75 unit tests: tools, chunker, RRF, citations, agent loop (fake LLM), sessions, Redis wrapper
./scripts/smoke.sh                  # health, ingest, cited answer, Redis outage (also runs in CI with a stub LLM)
./scripts/edge_cases.sh             # 23 edge cases (NO_LLM=1 skips the two that call Claude)
```

CI (`.github/workflows/ci.yml`): ruff → pytest → Kubernetes manifest render → frontend build →
`docker compose build` → smoke test with `LLM_PROVIDER=stub` (no API key) → persistence across
restart → push images to GHCR tagged with the commit SHA.
