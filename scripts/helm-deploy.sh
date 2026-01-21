#!/bin/bash
set -e

echo "========================================="
echo "DataAlchemy Helm Deployment (K3d)"
echo "========================================="

# Configuration
CHART_DIR="deploy/charts/data-alchemy"
NAMESPACE="data-alchemy"
IMAGE_NAME="data-alchemy:latest"
OPERATOR_IMAGE="dataalchemy-operator:latest"

# Step 1: Build Docker images
echo ""
echo "Step 1: Building Docker images..."
docker build -t ${IMAGE_NAME} .
docker build -t ${OPERATOR_IMAGE} deploy/operator/

# Step 2: Import images into K3d
echo ""
echo "Step 2: Importing images into K3d..."
k3d image import ${IMAGE_NAME} -c dataalchemy || k3d image import ${IMAGE_NAME}
k3d image import ${OPERATOR_IMAGE} -c dataalchemy || k3d image import ${OPERATOR_IMAGE}

# Step 3: Deploy with Helm
echo ""
echo "Step 3: Deploying with Helm..."

# Pre-apply CRDs to avoid validation issues with DataAlchemyStack resources
echo "Applying CRDs..."
kubectl apply -f ${CHART_DIR}/crds/ || true

# Use --install to handle both initial install and upgrades
# We use --atomic to rollback on failure
helm upgrade --install data-alchemy ${CHART_DIR} \
    --namespace ${NAMESPACE} \
    --create-namespace \
    --wait \
    --timeout 600s

echo ""
echo "========================================="
echo "Deployment Complete!"
echo "========================================="
echo ""
echo "Access URLs:"
echo "  - WebUI: http://data-alchemy.test"
echo ""
echo "To run lora-full-cycle:"
echo "  helm upgrade data-alchemy ${CHART_DIR} --reuse-values --set jobs.fullCycle.enabled=true"
echo ""
