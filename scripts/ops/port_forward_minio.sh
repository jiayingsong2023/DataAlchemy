#!/bin/bash
# Helper script to manage MinIO port-forward for Windows/k3d environments

set -e

SERVICE_NAME="${1:-dataalchemy-minio}"
NAMESPACE="${2:-default}"
LOCAL_PORT="${3:-9000}"
REMOTE_PORT="${4:-9000}"

echo "=========================================="
echo "MinIO Port-Forward Helper"
echo "=========================================="
echo ""
echo "Service: $SERVICE_NAME"
echo "Namespace: $NAMESPACE"
echo "Port mapping: localhost:$LOCAL_PORT -> $SERVICE_NAME:$REMOTE_PORT"
echo ""

# Check if kubectl is available
if ! command -v kubectl &> /dev/null; then
    echo "❌ kubectl not found. Please install kubectl."
    exit 1
fi

# Check if service exists
if ! kubectl get svc "$SERVICE_NAME" -n "$NAMESPACE" &>/dev/null; then
    echo "❌ Service '$SERVICE_NAME' not found in namespace '$NAMESPACE'"
    echo ""
    echo "Available MinIO services:"
    kubectl get svc -A | grep -i minio || echo "  (none found)"
    exit 1
fi

echo "✅ Service found. Starting port-forward..."
echo ""
echo "Press Ctrl+C to stop port-forward"
echo ""

# Start port-forward
kubectl port-forward -n "$NAMESPACE" "svc/$SERVICE_NAME" "$LOCAL_PORT:$REMOTE_PORT"
