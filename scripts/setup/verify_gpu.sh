#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${1:-data-alchemy}"
SELECTOR="${2:-app=webui}"

kubectl -n "$NAMESPACE" wait --for=condition=available deployment/webui --timeout=180s >/dev/null
# Select the newest desired ReplicaSet so a terminating old Pod cannot satisfy the gate.
RS="$(kubectl -n "$NAMESPACE" get rs -l "$SELECTOR" --sort-by=.metadata.creationTimestamp \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.replicas}{"\n"}{end}' \
  | awk '$2 == 1 {name=$1} END {print name}')"
if [[ -z "$RS" ]]; then
    echo "GPU gate failed: no desired WebUI ReplicaSet in namespace $NAMESPACE" >&2
    exit 1
fi
HASH="$(kubectl -n "$NAMESPACE" get rs "$RS" -o jsonpath='{.metadata.labels.pod-template-hash}')"
POD="$(kubectl -n "$NAMESPACE" get pods -l "$SELECTOR,pod-template-hash=$HASH" \
  --field-selector=status.phase=Running \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | head -n 1)"
if [[ -z "$POD" ]]; then
    echo "GPU gate failed: no running WebUI Pod in namespace $NAMESPACE" >&2
    exit 1
fi

echo "GPU gate: checking $NAMESPACE/$POD"
kubectl -n "$NAMESPACE" exec "$POD" -- python -c '
import torch
assert torch.cuda.is_available(), "torch.cuda.is_available() is false"
assert torch.cuda.device_count() > 0, "no ROCm device visible"
print({"cuda": True, "count": torch.cuda.device_count(), "hip": torch.version.hip})
'
