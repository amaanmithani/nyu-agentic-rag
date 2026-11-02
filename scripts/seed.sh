#!/usr/bin/env bash
# Uploads every PDF in corpus/pdfs/ through the public API (same path as the UI)
# and waits until each document is ready. Re-running is safe: duplicates are skipped.
set -euo pipefail
API="${API:-http://localhost:8000}"
cd "$(dirname "$0")/.."
# Log in first when the app is password-protected (APP_PASSWORD from the env or .env).
APP_PASSWORD="${APP_PASSWORD:-$(grep -E '^APP_PASSWORD=' .env 2>/dev/null | cut -d= -f2-)}"
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT
if [[ -n "$APP_PASSWORD" ]]; then
  python3 -c 'import json,sys; print(json.dumps({"password": sys.argv[1]}))' "$APP_PASSWORD" \
    | curl -fsS -c "$JAR" -H 'content-type: application/json' --data-binary @- "$API/auth/login" >/dev/null
fi

args=()
for f in corpus/pdfs/*.pdf; do
  # Optional display title from manifest.tsv (4th column); otherwise the PDF metadata is used.
  title="$(awk -F'\t' -v n="$(basename "$f")" '$1 == n {print $4}' corpus/manifest.tsv)"
  args+=(-F "files=@$f;type=application/pdf" -F "titles=$title")
done
resp="$(curl -b "$JAR" -fsS "${args[@]}" "$API/documents")"
echo "$resp" | python3 -m json.tool

for id in $(echo "$resp" | python3 -c 'import json,sys; print(" ".join(d["doc_id"] for d in json.load(sys.stdin)["documents"] if "doc_id" in d))'); do
  while :; do
    s="$(curl -b "$JAR" -fsS "$API/documents/$id/status")"
    st="$(echo "$s" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
    [[ "$st" == "processing" ]] || break
    sleep 2
  done
  echo "$s"
done
