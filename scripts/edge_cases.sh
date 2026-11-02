#!/usr/bin/env bash
# Edge-case checks against a running stack. No LLM calls except the two "limit" checks at
# the end (skip them with NO_LLM=1). Each line prints PASS/FAIL; exits non-zero on any FAIL.
set -uo pipefail
API="${API:-http://localhost:8000}"
cd "$(dirname "$0")/.."
# Log in first when the app is password-protected (APP_PASSWORD from the env or .env).
APP_PASSWORD="${APP_PASSWORD:-$(grep -E '^APP_PASSWORD=' .env 2>/dev/null | cut -d= -f2-)}"
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT
if [[ -n "$APP_PASSWORD" ]]; then
  python3 -c 'import json,sys; print(json.dumps({"password": sys.argv[1]}))' "$APP_PASSWORD" \
    | curl -fsS -c "$JAR" -H 'content-type: application/json' --data-binary @- "$API/auth/login" >/dev/null
fi
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP" "$JAR"' EXIT
fails=0
check() { # name, expected, actual
  if [[ "$3" == "$2" ]]; then echo "PASS  $1"; else echo "FAIL  $1 (expected $2, got $3)"; fails=$((fails+1)); fi
}
code() { curl -b "$JAR" -s -o "$TMP/body" -w '%{http_code}' "$@"; }
j() { python3 -c "import json,sys; d=json.load(open('$TMP/body')); print($1)"; }

echo "== uploads"
echo "hello" > "$TMP/notes.txt"
check "non-PDF upload rejected"            rejected "$(code -F "files=@$TMP/notes.txt" "$API/documents" >/dev/null; j 'd["documents"][0]["status"]')"
: > "$TMP/empty.pdf"
check "empty file rejected"                rejected "$(code -F "files=@$TMP/empty.pdf;type=application/pdf" "$API/documents" >/dev/null; j 'd["documents"][0]["status"]')"
check "duplicate PDF detected"             True "$(code -F "files=@corpus/pdfs/omb-m-25-22.pdf;type=application/pdf" "$API/documents" >/dev/null; j 'd["documents"][0]["duplicate"]')"
# A valid PDF with no extractable text (like a scan) must fail ingest cleanly, not crash.
uv run -q --no-project --with reportlab python -c "
from reportlab.pdfgen import canvas; c = canvas.Canvas('$TMP/scan.pdf'); c.rect(50, 50, 400, 600); c.showPage(); c.save()"
ID=$(code -F "files=@$TMP/scan.pdf;type=application/pdf" "$API/documents" >/dev/null; j 'd["documents"][0]["doc_id"]')
for _ in $(seq 1 30); do s=$(curl -b "$JAR" -s "$API/documents/$ID/status" | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])'); [[ $s == processing ]] || break; sleep 1; done
check "image-only PDF fails ingest cleanly" failed "$s"
curl -b "$JAR" -s -o /dev/null -X DELETE "$API/documents/$ID"
check "delete unknown document -> 404"     404 "$(code -X DELETE "$API/documents/does-not-exist")"

echo "== search + cache invalidation"
Q="Northbridge AI Review Board meeting time"
code -G "$API/search" --data-urlencode "q=$Q" >/dev/null; code -G "$API/search" --data-urlencode "q=$Q" >/dev/null
check "repeat search served from Redis cache" True "$(j 'd["cached"]')"
code -X DELETE "$API/documents/northbridge-ai-policy-synthetic" >/dev/null
code -G "$API/search" --data-urlencode "q=$Q" >/dev/null
check "upload/delete invalidates cache (corpus_version)" False "$(j 'd["cached"]')"
check "deleted document no longer retrievable" False "$(j 'any(h["doc_id"].startswith("northbridge") for h in d["hits"])')"
curl -b "$JAR" -s -o /dev/null -F "files=@corpus/pdfs/northbridge-ai-policy-SYNTHETIC.pdf;type=application/pdf" -F "titles=" "$API/documents"
for _ in $(seq 1 30); do s=$(curl -b "$JAR" -s "$API/documents/northbridge-ai-policy-synthetic/status" | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])'); [[ $s == processing ]] || break; sleep 1; done
check "re-upload restores document"        ready "$s"
check "SQL-ish input is just text"         200 "$(code -G "$API/search" --data-urlencode "q='; DROP TABLE chunks; --")"
check "search top_k out of range -> 422"   422 "$(code -G "$API/search" --data-urlencode "q=ai" --data-urlencode "top_k=99")"
check "empty search query -> 422"          422 "$(code -G "$API/search" --data-urlencode "q=")"

echo "== sessions + ask validation"
SID=$(code -X POST "$API/sessions" >/dev/null; j 'd["session_id"]')
OTHER=$(code -X POST "$API/sessions" >/dev/null; j 'd["session_id"]')
check "unknown session -> 404"             404 "$(code "$API/sessions/not-a-session")"
check "empty question -> 422"              422 "$(code -X POST "$API/sessions/$SID/ask" -H 'content-type: application/json' -d '{"question":""}')"
LONG=$(python3 -c 'print("x"*2001)')
check "over-long question -> 422"          422 "$(code -X POST "$API/sessions/$SID/ask" -H 'content-type: application/json' -d "{\"question\":\"$LONG\"}")"
check "bad mode -> 422"                    422 "$(code -X POST "$API/sessions/$SID/ask" -H 'content-type: application/json' -d '{"question":"q","mode":"magic"}')"
check "bad fault spec -> 400"              400 "$(code -X POST "$API/sessions/$SID/ask" -H 'content-type: application/json' -d '{"question":"q","fault_inject":"search_documents:explode:1"}')"
RUN=$(docker compose exec -T db psql -U rag -d rag -tAc "select id from runs where session_id is not null order by id desc limit 1")
OWNER=$(docker compose exec -T db psql -U rag -d rag -tAc "select session_id from runs where id=$RUN")
check "run visible to its own session"     200 "$(code "$API/sessions/$OWNER/runs/$RUN")"
check "run hidden from another session"    404 "$(code "$API/sessions/$OTHER/runs/$RUN")"

echo "== tools are read-only"
RO=$(docker compose exec -T api python -c "
from app.tools.corpus import _read
try:
    _read('DELETE FROM chunks'); print('wrote')
except Exception as e:
    print(type(e).__name__)")
check "tool DB access rejects writes"      ReadOnlySqlTransaction "$RO"

if [[ "${NO_LLM:-0}" != "1" ]]; then
  echo "== limits produce useful partial answers (2 small LLM calls)"
  OUT=$(docker compose exec -T -e MAX_STEPS=1 api python -m app.agent.cli \
        "Compare the CAIO deadlines, AI strategy deadlines and high-impact AI rules in M-24-10 and M-25-21." 2>/dev/null)
  check "MAX_STEPS=1 -> partial answer"    partial "$(echo "$OUT" | grep -oE '^=== [a-z]+' | head -1 | cut -c5-)"
  echo "$OUT" | grep -q "Not completed" && check "partial lists what was not completed" yes yes \
                                         || check "partial lists what was not completed" yes no
  OUT=$(docker compose exec -T -e RUN_DEADLINE_S=4 api python -m app.agent.cli \
        "What does the AI RMF say about each of its four functions?" 2>/dev/null)
  check "RUN_DEADLINE_S=4 -> stops with partial/answer, not a hang" 0 "$(echo "$OUT" | grep -cE '^=== (error)')"
  echo "      stop reason: $(echo "$OUT" | grep -oE 'stop: [a-z_]+')"
fi

echo
[[ $fails -eq 0 ]] && echo "ALL EDGE CASES PASSED" || echo "$fails FAILED"
exit $fails
