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
- **Citation verification** of every `[doc:page]` against what the tools returned in the run.
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
  (judge 11/12 vs 6/12), 0 fabricated citations, agent ≈1.8× slower and ≈1.7× more expensive.
  Plus 23 automated edge-case checks.

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
- **One eval run, 11 cases:** no variance estimate; mostly precise phrasing (vague phrasing tested
  manually on 3 questions). Case 4 was used for prompt tuning (not held out).
- **Agent case 8:** recovered from a failing tool but didn't tell the user; fixed by a prompt rule
  after the eval and spot-checked once, not re-evaluated.
- **Citation checker is lexical:** strict on numbers, weak on paraphrase and negative claims; a
  wrong year is not caught (dates/years are excluded to avoid false positives).
- **Reranker margin is narrow on policy text** (an out-of-corpus question scored −0.18 against a
  threshold of 0.0).
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

## Interview cheat sheet

**Key corpus facts** (PDF page index):

| Fact | Source |
|---|---|
| M-25-21 **rescinds and replaces** M-24-10 | omb-m-25-21 p.1 |
| Chief AI Officer within **60 days**, in both memos | omb-m-24-10 p.4 · omb-m-25-21 p.10 |
| CFO Act agency AI strategy: **365 days** (2024) → **180 days** (2025) | omb-m-24-10 p.9 · omb-m-25-21 p.5 |
| Terminology: "rights-/safety-impacting AI" (2024) → "**high-impact AI**" (2025) | omb-m-24-10 p.2 · omb-m-25-21 p.3 |
| High-impact AI: **pre-deployment testing** and an **AI impact assessment** before deployment | omb-m-25-21 p.15, p.16 |
| AI RMF Core has **four functions**: Govern, Map, Measure, Manage | nist-ai-rmf p.25 |
| GenAI Profile defines risks unique to or exacerbated by GenAI (e.g. confabulation, CBRN, data privacy, information security) | nist-genai-profile p.6–8 |
| AI Bill of Rights, five principles: Safe and Effective Systems; Algorithmic Discrimination Protections; Data Privacy; Notice and Explanation; Human Alternatives, Consideration, and Fallback | ai-bill-of-rights p.4–7 |
| SP 800-218A **augments the SSDF v1.1** for generative AI and dual-use foundation models | nist-sp800-218a p.5 |
| Synthetic Northbridge policy: claims **five** AI RMF functions (adds "Monitor"); p.3 holds the injection | northbridge-ai-policy-synthetic p.2, p.3 |

**Likely questions:**

- **Why not LangGraph?** One agent, a handful of tools and hard safety limits: a ~290-line loop
  makes every decision point explicit and lets me unit-test the loop with a scripted fake LLM.
  I'd reach for a graph framework for checkpointing, human-in-the-loop pauses or multiple agents.
- **What makes it "agentic" rather than RAG?** The model decides the next action from tool
  results: one search per sub-question, rephrasing on `weak`, reading neighbouring pages, asking
  for clarification, switching tools when search fails. The traces show each; the baseline that
  can't do these scores 5/11 vs 10/11.
- **How does the agent know a search was weak?** A cross-encoder rerank score below 0.0. I added
  the reranker because bi-encoder cosine scores were compressed (0.69 irrelevant vs 0.70
  relevant), and calibrated the threshold on in- and out-of-corpus queries.
- **Why 300-word chunks?** bge-small truncates at 512 tokens; 800-token chunks would be half
  invisible to vector search. Chunks never cross pages, so each cites exactly one page.
- **How do you stop hallucinated citations?** Every `[doc:page]` is checked against the pages
  the tools returned in that run; never-retrieved pages are flagged red and fail the eval. Result:
  0 fabricated citations in both modes. The numeric/lexical check catches wrong figures.
- **Prompt injection?** Retrieved text is wrapped as untrusted data, closing tags are escaped,
  suspicious passages are flagged, and tools can't write or reach anything outside the corpus. The
  planted injection was ignored and reported.
- **What happens if Redis dies?** 0.5 s timeouts plus a 15 s circuit breaker; no cache, state from
  Postgres (written through on every turn), lock skipped; `/health` says `degraded` with HTTP 200.
  Shown with `docker compose stop redis` and by scaling Redis to 0 on Kubernetes.
- **Why is the corpus version in Postgres?** Redis can evict keys; losing the version while cached
  results survive would serve stale results after an upload or delete.
- **Where does the agent not help?** Simple lookups: same answer, ≈1.8× latency and ≈1.7× cost.
  Next step: route those to the single-pass path.
- **What would you change for production at NYU?** Auth and per-team corpora, managed
  Postgres/Redis, OpenShift deployment with a Route and HPA, OpenTelemetry tracing, a larger eval
  built from real questions, and cost routing.
- **What went wrong along the way?** A redis-py `SET NX` return value made the lock never block
  (the unit test's fake returned `False`); a React effect returned `scrollIntoView`'s value and
  blanked the page in current Chrome; the citation checker flagged dates and memo numbers; my
  first eval rules missed correct refusals and conflicts. Each was found by testing the live
  system and fixed with a test.
