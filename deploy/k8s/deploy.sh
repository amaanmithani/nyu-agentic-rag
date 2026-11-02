#!/usr/bin/env bash
# Deploy to the current kubectl context (Docker Desktop by default) using the local overlay.
#   ./deploy/k8s/deploy.sh            # build images, create secret, apply, wait
#   ./deploy/k8s/deploy.sh --no-build # reuse existing local images
# Secrets are read from .env and piped straight to kubectl; they are never written to disk.
set -euo pipefail
cd "$(dirname "$0")/../.."
NS=agentic-rag
CTX="${KUBE_CONTEXT:-docker-desktop}"
k() { kubectl --context "$CTX" -n "$NS" "$@"; }

if [[ "${1:-}" != "--no-build" ]]; then
  docker compose build api web
fi

kubectl --context "$CTX" create namespace "$NS" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f -
grep -E '^(ANTHROPIC_API_KEY|POSTGRES_PASSWORD)=' .env \
  | k create secret generic rag-secrets --from-env-file=/dev/stdin --dry-run=client -o yaml \
  | k apply -f -

kubectl --context "$CTX" apply -k deploy/k8s/overlays/local
k rollout status statefulset/db --timeout=180s
k rollout status deployment/redis --timeout=120s
k rollout status deployment/api --timeout=600s
k rollout status deployment/web --timeout=120s
k get pods -o wide
echo "UI: http://localhost:30080   health: curl localhost:30080/api/health"
