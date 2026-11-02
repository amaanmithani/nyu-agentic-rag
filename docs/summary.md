# Summary

## What was built

An agentic RAG application for **AI risk and governance questions over versioned, superseded
policy documents** (NIST AI frameworks, OMB AI memoranda, the AI Bill of Rights, a synthetic
university policy), running entirely in Docker Compose:

- **RAG pipeline:** page-by-page PDF parsing, 300-word page-bounded chunks, local embeddings,
  pgvector + Postgres full-text hybrid search fused with RRF and reranked by a cross-encoder;
  every chunk keeps its document and page.
- **Adaptive agent:** a hand-written Claude tool-use loop that decomposes questions, rephrases
  after `weak` results, re-retrieves missing evidence, reads whole pages, asks targeted
  clarifications, recovers from tool failures, and combines and contrasts evidence across
  documents. Code enforces step, token and time limits and returns a useful partial answer when
  one is hit.
- **4 read-only tools** with Pydantic contracts, structured `ok/empty/weak/error/timeout`
  results, timeouts, bounded retries and fault injection.
- **Citation verification** in two layers: every `[doc:page]` is checked at runtime against the
  text the tools returned in the run (fabricated pages flagged red), and in the eval a small model
  reads each claim with the full cited page to judge whether the page supports it.
- **State and observability:** per-session turns, evidence, tool results and pending
  clarification (Redis + Postgres write-through); a live SSE execution trace in the UI; every run
  and step stored with timing, tokens and cost.
- **Safety:** untrusted-content wrapping with a break-out guard and injection flagging,
  read-only SQL for tools, isolated sessions, configurable limits.
- **Redis** for a search cache (invalidated by a Postgres corpus version), an embedding cache,
  session state, run locks and ingest status, with a circuit breaker and a Postgres fallback.
- **DevOps:** Dockerfiles (non-root, OpenShift-compatible), Compose with health checks and
  volumes, CI (lint, 75 tests, manifest render, builds, smoke test with a stub LLM, restart
  check, GHCR push), Kubernetes manifests deployed and tested on Docker Desktop, OpenShift overlay.
- **Evaluation:** 11 cases (all 8 required behaviours) through the public API, agent vs a
  single-pass baseline on the same corpus, model and prompt: **agent 10/11, baseline 5/11**
  (judge 11/12 vs 6/12; the agent's one failure passed when re-run after a prompt fix);
  citations semantically supported 94% vs 91% (0 vs 1 unsupported), 0 fabricated in either;
  agent ≈1.8× slower and ≈1.7× more expensive. Plus 23 automated edge-case checks and 5
  realistic-phrasing questions.

## What was prioritized, and why

1. **A working, containerized core first** (Compose + health checks + `/health` on day one), as
   the brief asks.
2. **Retrieval quality and the agent's decision signals**, because the agent can only adapt if a
   tool can tell it a result is weak. Measuring that bi-encoder scores couldn't separate relevant
   from irrelevant results led to adding the reranker and calibrating the `weak` threshold.
3. **Honest evaluation over a flattering one:** scorer bugs were fixed in the open, raw and
   re-scored results are both published, the agent's real failure is reported, and the ties and
   overhead are stated.
4. **Production paths second:** Kubernetes/OpenShift manifests were added after the required
   pieces were done and tested.

## Unfinished work and known limitations

- **OpenShift on a real cluster:** the overlay (Route, arbitrary-UID compatibility) was tested
  only on Kubernetes with an OpenShift-style UID; a Developer Sandbox deployment is in progress
  (see the runbook).
- **Autoscaling not demonstrated:** the HPA is defined, but Docker Desktop has no metrics-server.
- **One eval run, 11 cases:** no variance estimate. The scored cases use precise phrasing;
  realistic vague phrasing was tested separately on 5 questions. Case 4 was used for prompt
  tuning (not held out). Case 8 failed in the main run and passed when re-run after a prompt fix.
- **Relevance scores are a signal, not a verdict:** vague questions can score below the `weak`
  threshold while the answer is present, and unanswerable questions about real topics can score
  above it; measured on two rerankers, neither separates them. The agent handles this by reading
  the returned passages and rephrasing, which worked in every observed case.
- **No cross-session learning:** the agent adapts within a question (and keeps context within a
  session) but does not learn from past answers or feedback.
- **No authentication or per-user corpora**; one shared library.
- **Single-node storage** (`ReadWriteOnce`) and FastAPI background tasks for ingest; no OCR for
  scanned PDFs.
- **Same-session follow-up detection:** when a clarification is pending, the next message is
  always treated as the reply (the agent handles an unrelated question gracefully, but there is
  no explicit "new question" detection).

## Next steps with more time

1. **Retrieval:** structure-aware chunking (headings, tables), a larger reranker, and a labelled
   retrieval set to track recall@k separately from answer quality.
2. **Evaluation:** 30–50 cases including vague, real-user phrasing; repeated runs to report
   variance; held-out tuning/test split; cost-per-correct-answer as a metric.
3. **Routing:** send simple lookups to the cheaper single-pass path and escalate to the agent on
   weak results or multi-part questions, since the eval shows the baseline ties on simple lookups.
4. **Feedback loop:** collect thumbs-up/down and accepted answers; reuse successful query
   rewrites; build eval cases from real traffic.
5. **Production hardening:** authentication and per-user/per-team corpora, OpenTelemetry tracing,
   managed Postgres/Redis, models baked into the image, a worker queue for ingest,
   metrics-server/HPA, budget alerts.
