#!/usr/bin/env bash
# End-to-end smoke test against a running compose stack. Used by CI (with LLM_PROVIDER=stub,
# so no API key is needed) and by the runbook after a deploy. Exits non-zero on any failure.
#   1. /health is ok      2. ingest one PDF     3. ask one question, get a cited answer
#   4. Redis outage: health degrades, questions still work, state falls back to Postgres
set -euo pipefail
API="${API:-http://localhost:8000}"
WEB="${WEB:-http://localhost:8080}"
# How to take Redis down/up for the outage check (defaults: Docker Compose). For Kubernetes:
#   REDIS_STOP="kubectl -n agentic-rag scale deploy/redis --replicas=0 && kubectl -n agentic-rag wait --for=delete pod -l app=redis"
#   REDIS_START="kubectl -n agentic-rag scale deploy/redis --replicas=1"
REDIS_STOP="${REDIS_STOP:-docker compose stop redis}"
REDIS_START="${REDIS_START:-docker compose start redis}"
cd "$(dirname "$0")/.."

j() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

echo "1) health"
for _ in $(seq 1 90); do
  curl -fsS "$API/health" 2>/dev/null | grep -q '"models":"ready"' && break
  sleep 2
done
curl -fsS "$API/health" | tee /dev/stderr | grep -q '"status":"ok"' || fail "health not ok"
curl -fsS "$WEB/api/health" >/dev/null || fail "web -> api proxy"
echo

echo "2) ingest"
PDF=corpus/synthetic/northbridge-ai-policy-SYNTHETIC.pdf
DOC=$(curl -fsS -F "files=@$PDF;type=application/pdf" "$API/documents" | j 'd["documents"][0]["doc_id"]')
for _ in $(seq 1 60); do
  STATUS=$(curl -fsS "$API/documents/$DOC/status" | j 'd["status"]')
  [[ "$STATUS" == "processing" ]] || break
  sleep 2
done
[[ "$STATUS" == "ready" ]] || fail "ingest status $STATUS"
echo "   $DOC ready"

echo "3) ask"
SID=$(curl -fsS -X POST "$API/sessions" | j 'd["session_id"]')
ASK() {
  curl -fsS -X POST "$API/sessions/$SID/ask?stream=false" -H 'content-type: application/json' \
    -d "{\"question\": \"$1\"}" | j '[e["data"] for e in d["events"] if e["type"]=="run_finished"][0]["answer"]'
}
ANSWER=$(ASK "When does the Northbridge AI Review Board meet?")
echo "   $ANSWER"
[[ "$ANSWER" == *"[$DOC:"* ]] || fail "answer has no citation to $DOC"

echo "4) redis outage"
eval "$REDIS_STOP" >/dev/null 2>&1
curl -fsS "$API/health" | tee /dev/stderr | grep -q '"redis":"degraded"' || fail "health not degraded"
echo
ANSWER=$(ASK "What does the policy say about approving generative AI tools?")
[[ -n "$ANSWER" ]] || fail "no answer with redis down"
SOURCE=$(curl -fsS "$API/sessions/$SID" | j 'd["state_source"]')
[[ "$SOURCE" == "postgres" ]] || fail "state source $SOURCE"
echo "   answered with redis down; session state served from $SOURCE"
eval "$REDIS_START" >/dev/null 2>&1

echo "SMOKE OK"
