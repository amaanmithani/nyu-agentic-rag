# Deployment runbook

All outputs below are copied from real runs on a MacBook (Docker Desktop 29.2, Compose v5,
8 CPUs / 8 GB for Docker) unless marked otherwise.

## Prerequisites

- Docker Desktop with Compose v2+ (≈8 GB RAM for Docker: the API holds ≈1.5 GB of models)
- [uv](https://docs.astral.sh/uv/) (Python tooling for tests, seeding and the eval) and `curl`
- An Anthropic API key (not needed for CI or the smoke test with `LLM_PROVIDER=stub`)
- For Kubernetes: Docker Desktop's built-in Kubernetes (or any cluster) and `kubectl`; for OpenShift: `oc`

## Configuration and secrets

| What | Where | Notes |
|---|---|---|
| All settings | `.env` (copy of [.env.example](../.env.example)) | Model ids, limits (`MAX_STEPS`, `TOOL_TIMEOUT_S`, `TOOL_MAX_RETRIES`, `MAX_TOKENS_PER_RUN`, `RUN_DEADLINE_S`), `ALLOW_FAULT_INJECTION`, `IMAGE_TAG`. |
| Secrets | `ANTHROPIC_API_KEY`, `POSTGRES_PASSWORD` in `.env` | `.env` is gitignored (`.env.*` too, except `.env.example`). Compose injects it at runtime via `env_file`; **no secret is baked into an image**. Every commit was checked for `sk-ant-` strings before pushing. |
| Kubernetes | Secret `rag-secrets` | `deploy/k8s/deploy.sh` pipes the two lines from `.env` into `kubectl create secret … --from-env-file=/dev/stdin`, so the values are never written to disk or the repo. Non-secret settings: ConfigMap `rag-config`. |
| Production | | Set `ALLOW_FAULT_INJECTION=false` (requests with `fault_inject` are then rejected with 400). Use a secret manager (OpenShift/K8s Secrets backed by Vault or a cloud KMS). |

## Deploy (Docker Compose)

```bash
cp .env.example .env && $EDITOR .env      # ANTHROPIC_API_KEY, POSTGRES_PASSWORD
./corpus/download.sh                      # fetch + SHA256-verify the 10 public PDFs
docker compose up -d --build              # db → redis → api (waits for healthy deps) → web
./scripts/seed.sh                         # upload the corpus via the API; waits until ready
```

Services: `db` (pgvector/pgvector:pg16, volume `pgdata`), `redis` (redis:7-alpine, no
persistence, `allkeys-lru` 256 MB), `api` (FastAPI :8000, volumes `uploads`, `model-cache`),
`web` (nginx-unprivileged :8080). Each has a health check; `api` starts only when `db` and
`redis` are healthy, and `web` only when `api` is healthy. Images run as non-root (api uid 1001,
group 0).

## Verify

```text
$ docker compose ps
SERVICE   STATUS
api       Up 2 hours (healthy)
db        Up 2 hours (healthy)
redis     Up 2 hours (healthy)
web       Up 2 hours (healthy)

$ curl -s localhost:8000/health          # same through the UI origin: localhost:8080/api/health
{"status":"ok","db":"ok","redis":"ok","models":"ready"}
```

`/health` returns **503** only if Postgres is down; a Redis outage is `"redis":"degraded"` with
HTTP 200; `models` is `loading` for the first ~30 s after start while the embedding and rerank
models load.

```bash
./scripts/smoke.sh        # health → ingest one PDF → ask (cited answer) → Redis outage → SMOKE OK
./scripts/edge_cases.sh   # 23 checks; NO_LLM=1 skips the two that call Claude
```

`edge_cases.sh` result (all passed): non-PDF and empty uploads rejected, duplicate detected,
image-only PDF fails cleanly, unknown delete → 404, repeat search served from Redis, upload/delete
invalidates the cache, deleted document no longer retrievable, SQL-ish input treated as text,
422s for bad `top_k` / empty query / empty or >2,000-char question / bad mode, 400 for a bad fault
spec, unknown session → 404, a run is visible to its own session and **404 from another session**,
tool DB access rejects writes (`ReadOnlySqlTransaction`), `MAX_STEPS=1` → partial answer with a
"Not completed" list, `RUN_DEADLINE_S=4` → stops with `stop: deadline`.

## A successful end-to-end request

```bash
SID=$(curl -s -X POST localhost:8000/sessions | python3 -c 'import json,sys;print(json.load(sys.stdin)["session_id"])')
curl -s -X POST "localhost:8000/sessions/$SID/ask?stream=false" -H 'content-type: application/json' \
  -d '{"question":"How quickly must agencies designate a Chief AI Officer under the 2024 and 2025 OMB memos?"}'
# Without ?stream=false the same endpoint streams every trace event as Server-Sent Events.
```

Observed (trace summarized, answer excerpt):
```text
call   search_documents {"query": "designate Chief AI Officer within days OMB memo", "top_k": 10}
->     ok: 10 passages, best score 7.86 [omb-m-24-10:4, omb-m-24-10:34, omb-m-25-21:13, ...]
==     answered | tools=1 6.3s $0.0278 | 0 fabricated citations
Both memos use the same deadline: **60 days** after the memo is issued.
- **OMB M-24-10 (March 28, 2024):** The head of each agency must designate a CAIO within 60 days
  of the memo's issuance [omb-m-24-10:4]. ...
- **OMB M-25-21 (2025):** The head of each agency must "retain or designate" a CAIO within 60 days
  of the memo's issuance [omb-m-25-21:10]. ...
```

The saved run and its trace: `GET /sessions/$SID/runs` and `GET /sessions/$SID/runs/{run_id}`.

## Persistence across restart

```bash
docker compose down && docker compose up -d     # containers removed, named volumes kept
```

Verified (on the first, health-guidance corpus; same mechanism): after `down`/`up`, all **9
documents / 1,095 chunks** were still there; an existing session loaded with all its turns
(`state_source: postgres`, since Redis has no persistence); run 5 and its **9 trace events** were
still readable. CI repeats this check on every push (`docker compose down` → `up` → documents
still listed). Kubernetes: see below (pods deleted, data kept).

## Redis outage

```bash
docker compose stop redis
curl -s localhost:8000/health
# {"status":"degraded","db":"ok","redis":"degraded","models":"ready"}   (HTTP 200)
docker compose start redis
# {"status":"ok","db":"ok","redis":"ok","models":"ready"}               (recovers on its own)
```

Observed with Redis stopped: session state loaded **from Postgres** (`state_source: postgres`,
3 turns); a question was answered normally (4.9 s); search still worked (no cache); ingest
status fell back to `documents.status` (`"source": "postgres"`); the per-session lock is skipped
(degraded). Mechanism: 0.5 s timeouts and a 15 s circuit breaker in `api/app/redis.py`. Keys and
TTLs: [architecture.md → Redis keys](architecture.md#redis-keys). Also automated in
`scripts/smoke.sh` (Compose, CI, Kubernetes).

## Update

Images are tagged by `IMAGE_TAG` (`agentic-rag-api:${IMAGE_TAG}`, `agentic-rag-web:${IMAGE_TAG}`).

```bash
git pull
export IMAGE_TAG=$(git rev-parse --short=12 HEAD)
docker compose build && docker compose up -d       # api waits for db/redis health; schema migrations are idempotent
curl -s localhost:8000/health && ./scripts/smoke.sh
```

CI pushes the images of every commit on `main` to GHCR as
`ghcr.io/amaanmithani/nyu-agentic-rag-{api,web}:<12-char sha>` and `:latest`, which the
Kubernetes/OpenShift manifests reference. To deploy a CI-built image with Compose instead of
building locally:

```bash
SHA=<12-char sha>
for s in api web; do docker pull ghcr.io/amaanmithani/nyu-agentic-rag-$s:$SHA && \
  docker tag ghcr.io/amaanmithani/nyu-agentic-rag-$s:$SHA agentic-rag-$s:$SHA; done
IMAGE_TAG=$SHA docker compose up -d
```

## Rollback

```bash
IMAGE_TAG=<previous sha> docker compose up -d     # Compose: point back at the previous image tag
kubectl -n agentic-rag rollout undo deploy/api    # Kubernetes/OpenShift (oc rollout undo …)
```

The database schema is additive (`CREATE … IF NOT EXISTS`), so an older API runs against a newer
schema. The Kubernetes rollback was executed (below); the Compose rollback uses the same tag
mechanism but was not separately exercised.

## Logging and observability

- **Structured run log:** one JSON line per question:
  ```text
  $ docker compose logs api | grep run_done
  run_done {"run_id": 11, "status": "answered", "stop_reason": "answer", "steps": 2, "tool_calls": 1, "tokens": 8288, "cost_usd": 0.005608, "latency_ms": 5474}
  ```
- **Trace store:** `runs` (status, stop reason, tokens in/out/cache read/cache write, cost,
  latency, model, citation report) and `trace_events` (one row per LLM call / tool call / tool
  result / limit / clarification, with step, tool, status, ms, payload). The UI's trace panel
  replays a saved run from these tables.
- Warnings are logged for each failed tool attempt (`tool search_documents attempt 1 failed: timed
  out after 5.0s`) and for the Redis circuit breaker opening.

## Scaling approach

- **API is stateless**: sessions and locks live in Redis + Postgres, uploads and models on
  volumes, so replicas can be added freely (Kubernetes runs 2, with an HPA from 2 to 5 at 70% CPU;
  embedding and reranking are the CPU-bound parts).
- **Shared state:** managed Postgres (backups, read replica) and managed Redis in production.
- **Storage:** single-node `ReadWriteOnce` volumes here; multi-node needs `ReadWriteMany` storage
  or object storage for uploads, and the models baked into the API image instead of a shared cache.
- **Ingest:** today FastAPI background tasks; at scale, a Redis-backed worker queue so ingestion
  scales separately and survives API restarts.
- **LLM cost/latency:** prompt caching is on; the per-run token budget caps worst-case cost; the
  baseline path (~$0.02, ~5 s) is an option for simple lookups.

## CI

`.github/workflows/ci.yml`, on every push and PR:

1. `ruff check` + `ruff format --check` (api + eval), `pytest` (75 tests: tools, chunker, RRF,
   citation verifier, agent loop with a scripted fake LLM, sessions, Redis wrapper), render the
   Kubernetes and OpenShift manifests with `kubectl kustomize`, frontend type-check + build.
2. `docker compose build`, `docker compose up --wait`, `scripts/smoke.sh` with **`LLM_PROVIDER=stub`**
   (the full loop, tools, DB and citation checks run without an API key), persistence across
   `down`/`up`, logs on failure.
3. On `main`: push both images to GHCR tagged with the commit SHA and `latest`.

All runs on `main` have been green since CI was added.

## Kubernetes (Docker Desktop)

```bash
./deploy/k8s/deploy.sh --no-build                       # namespace, Secret from .env, apply, wait
API=http://localhost:30080/api ./scripts/seed.sh        # fresh DB: ingest the corpus
K="kubectl --context docker-desktop -n agentic-rag"
```

Layout: `deploy/k8s/base` (Postgres StatefulSet + 2 Gi PVC, Redis, `api` ×2 with
startup/readiness/liveness probes, `preStop` drain, resource requests/limits, HPA, `web` ×2,
ConfigMap) and `deploy/k8s/overlays/local` (local images, `web` NodePort **30080**, and
`runAsUser: 1000680000`, `runAsGroup: 0` to mimic OpenShift's restricted SCC). Probes: `startup`
and `readiness` use `/health` (503 when Postgres is down takes the pod out of the Service);
`liveness` is a TCP check, so a database outage never restarts healthy API pods.

Measured results:

| Check | Result |
|---|---|
| Deploy | all 6 pods Running (api ×2, web ×2, db-0, redis) |
| Arbitrary UID | `id` in api and web pods → `uid=1000680000 gid=0(root)`; the API wrote 152 MB of models to its volume under that UID |
| Ingest | 11 documents, 1,106 chunks |
| Smoke test (`REDIS_STOP="$K scale deploy/redis --replicas=0 …"`) | `SMOKE OK`: cited answer; with Redis at 0 replicas health was `degraded`, the question was answered and state came from Postgres; back to `ok` after scaling up |
| Rolling update (`$K set image deploy/api api=agentic-rag-api:<sha>`) while polling `/health` every 0.2 s | first try: 354 ok / 4 failed; after adding a 5 s `preStop` drain: **378 ok / 1 failed** (the remaining failure is most likely a 2 s client timeout while the new pod loads its models on a single-node laptop) |
| Rollback (`$K rollout undo deploy/api`) | back to `agentic-rag-api:local`, rollout complete |
| Persistence | after deleting `db-0` and both api pods: health ok, **11 documents / 1,106 chunks** still present |
| HPA | created; metrics show `<unknown>` locally because Docker Desktop ships without metrics-server. **Autoscaling was demonstrated on OpenShift** (2→5 replicas, see below). |

To pause: `kubectl -n agentic-rag scale deploy --all --replicas=0` and
`kubectl -n agentic-rag scale statefulset db --replicas=0` (delete the HPA first, or it scales
`api` back to 2). Volumes are kept.

## OpenShift

`deploy/openshift` = the same base manifests + an edge-TLS **Route** for `web` (the UI proxies
`/api` internally, so only `web` is exposed). No `runAsUser` is set: OpenShift's restricted SCC
assigns a random UID, which the images are built for (non-root, group-0-writable directories,
nginx on 8080). Postgres: `pgvector/pgvector:pg16` also runs under an arbitrary UID because
`PGDATA` is a subdirectory the process creates itself (verified locally with
`docker run --user 1000680000:0`: initdb, startup and `CREATE EXTENSION vector` succeed). For
production, a managed Postgres or an operator (CloudNativePG / Crunchy) is still recommended for
backups and failover.

```bash
oc new-project agentic-rag
grep -E '^(ANTHROPIC_API_KEY|POSTGRES_PASSWORD)=' .env | oc create secret generic rag-secrets --from-env-file=/dev/stdin
oc apply -k deploy/openshift
oc rollout status deploy/api
oc get route web          # https://web-agentic-rag.apps.<cluster>/
API=https://$(oc get route web -o jsonpath='{.spec.host}')/api ./scripts/seed.sh
oc rollout undo deploy/api   # rollback
```

Images come from GHCR (`ghcr.io/amaanmithani/nyu-agentic-rag-{api,web}:latest`, built for
linux/amd64 by CI); the cluster needs pull access (public packages or an image pull secret).

### OpenShift Developer Sandbox results

Deployed to a real OpenShift cluster (Red Hat Developer Sandbox, project `amaanmithani-dev`,
multi-node AWS) with the **same images CI publishes to GHCR** and the `deploy/openshift` overlay.

```bash
oc login --token=<from console> --server=https://api.<cluster>:6443   # token kept in .env, never committed
./deploy/openshift/deploy.sh          # Secret from .env via stdin, oc apply -k, wait for rollouts
API=https://<route-host>/api ./scripts/seed.sh
API=https://<route-host>/api WEB=https://<route-host> \
  REDIS_STOP="oc scale deploy/redis --replicas=0 && oc wait --for=delete pod -l app=redis" \
  REDIS_START="oc scale deploy/redis --replicas=1" ./scripts/smoke.sh
```

| Check | Result |
|---|---|
| All pods admitted under OpenShift's **`restricted-v2` SCC** | ✅ api ×2, web ×2, db, redis; OpenShift assigned its own UID `1011910000` (gid 0) to every pod, no `runAsUser` in our manifests |
| Postgres (pgvector) and Redis under the arbitrary UID | ✅ running, data persisted on a `gp3` volume |
| **Route** (edge TLS) | ✅ `https://web-<project>.apps.<cluster>`, valid Let's Encrypt certificate; plain HTTP → **302** to HTTPS |
| Health over HTTPS | ✅ `{"status":"ok","db":"ok","redis":"ok","models":"ready"}` |
| Fault injection on the public URL | ✅ rejected: HTTP 400 `fault injection is disabled (ALLOW_FAULT_INJECTION=false)` (the overlay sets it to false) |
| Corpus ingested through the public URL | ✅ 11 docs, 606 pages, 1,106 chunks (≈9.7 min on the sandbox's shared CPUs vs ≈2 min locally) |
| **Autoscaling (HPA)** | ✅ ingestion drove the API to ~200% of its CPU request; HPA events: *"New size: 4; reason: cpu resource utilization above target"* → *"New size: 5"* (the max) |
| Smoke test over HTTPS incl. **Redis outage** | ✅ `SMOKE OK`: cited answer, injection reported as ignored; with Redis scaled to 0, `/health` = degraded, question answered, session state from Postgres |
| **Rolling update** `oc set image deploy/api api=…:ccc21eb42bca` | ✅ **341/341** health checks over HTTPS succeeded during the rollout (5 replicas) |
| **Rollback** `oc rollout undo deploy/api` | ✅ back to `:latest`, **338/338** health checks succeeded |

**Bug found only on the real cluster, and fixed.** When the HPA scaled the API from 2 to 5, three
new pods landed on other nodes and hung in `ContainerCreating` with *"Multi-Attach error for
volume … already used by pod(s) …"*: the `uploads` and `model-cache` volumes were
`ReadWriteOnce` (an EBS volume attaches to one node). Docker Desktop has a single node, so it
could not show this. Neither volume needed sharing (an uploaded PDF is only read by the ingest
task in the same pod; each pod can download its own models), so both became per-pod `emptyDir`
and the API is now stateless. After `oc apply`, all 5 replicas ran on **5 different nodes**;
Postgres was untouched (revision 1, 0 restarts) and all 11 documents remained. Trade-off: each
new API pod downloads ~150 MB of models at startup (production: bake them into the image).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/health` shows `"models":"loading"` | First start downloads ~200 MB of models (Compose: into the `model-cache` volume; Kubernetes/OpenShift: per pod); wait ~30 s. |
| Port 5432 already in use by another project | Not a conflict: our `db` is not published to the host (internal network only). Only 8000 and 8080 are published. |
| API container slow or killed | Docker needs ≈8 GB; the API holds ≈1.5 GB of models. Don't run the Compose stack and the Kubernetes copy at full size at the same time. |
| `ask` returns 409 | A question is already running in that session (Redis lock); wait or use a new session. |
| `ask` returns 503 "ANTHROPIC_API_KEY is not set" | Set the key in `.env` and `docker compose up -d` (env is read at container creation). |
| `fault_inject` rejected with 400 | `ALLOW_FAULT_INJECTION=false`, or a malformed spec (`tool:timeout|error:count`). |
| Upload status `failed: no extractable text` | Scanned/image-only PDF; OCR is not supported. |
