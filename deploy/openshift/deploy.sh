#!/usr/bin/env bash
# Deploy to the OpenShift project you are logged in to (`oc login ...` first).
# The Secret is created from .env via stdin, so the API key never touches disk or git.
set -euo pipefail
cd "$(dirname "$0")/../.."
NS="$(oc project -q)"
echo "deploying into project: $NS"
grep -E '^(ANTHROPIC_API_KEY|POSTGRES_PASSWORD)=' .env \
  | oc create secret generic rag-secrets --from-env-file=/dev/stdin --dry-run=client -o yaml \
  | oc apply -f -
oc apply -k deploy/openshift
oc rollout status statefulset/db --timeout=300s
oc rollout status deployment/redis --timeout=180s
oc rollout status deployment/api --timeout=900s
oc rollout status deployment/web --timeout=300s
oc get pods
echo "URL: https://$(oc get route web -o jsonpath='{.spec.host}')"
