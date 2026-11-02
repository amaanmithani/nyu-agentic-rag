#!/usr/bin/env bash
# Downloads the public corpus listed in manifest.tsv into corpus/pdfs/ and verifies
# each file against its pinned SHA256, so everyone indexes byte-identical documents.
# The synthetic test document is committed in corpus/synthetic/ (see SOURCES.md).
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p pdfs

if command -v sha256sum >/dev/null; then sha() { sha256sum "$1" | cut -d' ' -f1; }
else sha() { shasum -a 256 "$1" | cut -d' ' -f1; }; fi

failed=0
while IFS=$'\t' read -r name expected url; do
  [[ "$name" == "filename" ]] && continue
  if [[ ! -f "pdfs/$name" ]]; then
    echo "downloading $name"
    # Browser UA: some .gov hosts reject curl's default. Retries cover flaky mirrors.
    curl -fsSL --retry 3 -A "Mozilla/5.0" -o "pdfs/$name" "$url"
  fi
  actual="$(sha "pdfs/$name")"
  if [[ "$actual" == "$expected" ]]; then
    echo "ok       $name"
  else
    echo "MISMATCH $name (expected $expected, got $actual)" >&2
    failed=1
  fi
done < manifest.tsv

cp synthetic/*.pdf pdfs/
echo "copied synthetic test document(s) into pdfs/"
exit $failed
