#!/bin/bash
set -e

echo "========================================="
echo "DataAlchemy K3d Deployment Script"
echo "========================================="

# Configuration
IMAGE_NAME="data-alchemy:latest"
NAMESPACE="data-alchemy"
MANIFESTS_DIR="deploy/k3d"

# Step 1: Build Docker image
echo ""
echo "Step 1: Building unified Docker image..."
docker build -t ${IMAGE_NAME} .

# Step 2: Import images into K3d
echo ""
echo "Step 2: Importing image into K3d..."
k3d image import ${IMAGE_NAME} -c dataalchemy || {
    echo "Warning: Failed to import to 'dataalchemy'. Trying default cluster..."
    k3d image import ${IMAGE_NAME}
}

# Step 3: Apply Kubernetes manifests
echo ""
echo "Step 3: Applying Kubernetes manifests..."
kubectl apply -f ${MANIFESTS_DIR}/01-namespace.yaml
kubectl apply -f ${MANIFESTS_DIR}/02-minio.yaml
kubectl apply -f ${MANIFESTS_DIR}/03-redis.yaml
kubectl apply -f ${MANIFESTS_DIR}/04-pvc.yaml
kubectl apply -f ${MANIFESTS_DIR}/05-webui.yaml
kubectl apply -f ${MANIFESTS_DIR}/06-coordinator.yaml

echo ""
echo "Step 4: Waiting for deployments to be ready..."
kubectl wait --for=condition=available --timeout=300s \
    deployment/minio deployment/redis deployment/webui deployment/coordinator \
    -n ${NAMESPACE}

# Step 5: Display access information
echo ""
echo "========================================="
echo "Deployment Complete!"
echo "========================================="
echo ""
echo "Access URLs (add to /etc/hosts if needed):"
echo "  - WebUI:         http://data-alchemy.test"
echo "  - MinIO API:     http://minio.test"
echo "  - MinIO Console: http://minio-console.test"
echo ""
echo "MinIO Credentials:"
echo "  - Access Key: admin"
echo "  - Secret Key: minioadmin"
echo ""
echo "Useful Commands:"
echo "  - View pods:     kubectl get pods -n ${NAMESPACE}"
echo "  - View logs:     kubectl logs -f deployment/webui -n ${NAMESPACE}"
echo "  - Full Cycle:    kubectl apply -f ${MANIFESTS_DIR}/08-full-cycle-job.yaml"
echo ""
echo "To upload data to MinIO:"
echo "  python scripts/ops/manage_minio.py upload <file>"
echo ""
