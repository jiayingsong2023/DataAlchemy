#!/bin/bash
# Quick restart k3d cluster and fix kubeconfig

set -e

echo "🔄 Restarting k3d cluster..."
k3d cluster stop dataalchemy 2>/dev/null || true
sleep 2
k3d cluster start dataalchemy

echo "📝 Updating kubeconfig..."
k3d kubeconfig get dataalchemy > ~/.kube/config
# Fix 0.0.0.0 to 127.0.0.1 for local connection
sed -i 's|server: https://0.0.0.0:|server: https://127.0.0.1:|' ~/.kube/config

echo "⏳ Waiting for cluster to be ready..."
sleep 5

echo "✅ Cluster status:"
kubectl get nodes
kubectl get pods
