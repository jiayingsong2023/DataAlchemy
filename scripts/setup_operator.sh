#!/bin/bash
# DataAlchemy Operator Deployment Script (Ubuntu/Linux)
# Cross-platform replacement for setup_operator.ps1

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m' # No Color

# Check for cleanup flag
if [[ "$1" == "--cleanup" ]] || [[ "$1" == "-c" ]]; then
    echo -e "${MAGENTA}🧹 Cleaning up DataAlchemy environment...${NC}"
    
    # 1. Delete the Custom Resource
    echo "Deleting Custom Resources..."
    kubectl delete das --all --ignore-not-found=true --timeout=30s || true
    
    # 2. Delete Infrastructure (Operator, RBAC, CRD)
    echo "Deleting Infrastructure..."
    kubectl delete -f deploy/operator/manifests.yaml --ignore-not-found=true || true
    
    # 3. Delete Secrets and shared SA
    echo "Deleting Secrets..."
    kubectl delete secret dataalchemy-secret --ignore-not-found=true || true
    kubectl delete sa spark --ignore-not-found=true || true
    
    # 4. Cleanup orphaned resources
    echo "Removing orphaned resources..."
    kubectl delete pods,svc,deploy,jobs,ingress -l "dataalchemy.io/managed=true" --ignore-not-found=true || true
    kubectl delete pods,svc,deploy,jobs,ingress -l "stack=dataalchemy" --ignore-not-found=true || true
    kubectl delete pods,svc,deploy,jobs,ingress -l "stack=test-stack" --ignore-not-found=true || true
    
    # 5. Optional: Clean up local hostPath data
    echo ""
    read -p "❓ Do you also want to WIPE all ephemeral data on local disk (MinIO, Redis, RAG Index)? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${RED}🗑️  Wiping specific local data directories...${NC}"
        if [ -d "data/minio_data" ]; then
            rm -rf data/minio_data/*
        fi
        if [ -d "data/redis_data" ]; then
            rm -rf data/redis_data/*
        fi
        
        echo "🧹 Clearing RAG index files (faiss_index, metadata.db)..."
        find data -name "faiss_index.bin" -o -name "metadata.db*" | xargs rm -f 2>/dev/null || true
        
        echo -e "${GREEN}✅ Local ephemeral data wiped. (users.db preserved)${NC}"
    fi
    
    echo -e "${GREEN}✨ Cleanup complete!${NC}"
    exit 0
fi

echo -e "${CYAN}🚀 Starting DataAlchemy Operator Deployment...${NC}"

# Get project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Detect platform
PLATFORM=$(uname -s)
echo -e "${YELLOW}Detected platform: $PLATFORM${NC}"

# Set up kubeconfig for k3d if k3d cluster exists
if command -v k3d &> /dev/null && k3d cluster list 2>/dev/null | grep -q "dataalchemy"; then
    echo -e "${CYAN}🔧 Setting up kubeconfig for k3d cluster...${NC}"
    export KUBECONFIG=$(k3d kubeconfig write dataalchemy)
    echo -e "${GREEN}✅ KUBECONFIG set to: $KUBECONFIG${NC}"
    
    # Fix kubeconfig: replace 0.0.0.0 with 127.0.0.1 (0.0.0.0 doesn't work for kubectl)
    if [ -f "$KUBECONFIG" ]; then
        sed -i 's|server: https://0.0.0.0:|server: https://127.0.0.1:|g' "$KUBECONFIG"
        echo -e "${CYAN}🔧 Fixed kubeconfig server address (0.0.0.0 -> 127.0.0.1)${NC}"
    fi
    
    # Wait a moment for kubeconfig to be ready
    sleep 1
    
    # Verify cluster connection with better error handling
    if ! command -v kubectl &> /dev/null; then
        echo -e "${RED}❌ kubectl not found. Please install kubectl.${NC}"
        exit 1
    fi
    
    # Try to connect (with retry)
    CONNECTED=false
    for i in {1..3}; do
        if kubectl cluster-info >/dev/null 2>&1; then
            CONNECTED=true
            break
        fi
        if [ $i -lt 3 ]; then
            echo -e "${YELLOW}⚠️  Connection attempt $i failed, retrying...${NC}"
            sleep 2
        fi
    done
    
    if [ "$CONNECTED" = false ]; then
        echo -e "${RED}❌ Failed to connect to k3d cluster after 3 attempts.${NC}"
        echo -e "${YELLOW}Debug info:${NC}"
        echo "   KUBECONFIG: $KUBECONFIG"
        echo "   Cluster status:"
        k3d cluster list 2>/dev/null || true
        echo "   Manual test: kubectl cluster-info"
        exit 1
    fi
    echo -e "${GREEN}✅ Successfully connected to k3d cluster${NC}"
fi

# 1. Build and Load Docker Images
echo -e "\n${YELLOW}[1/3] Building DataProcessor and Operator images...${NC}"
docker build -t data-processor:latest ./data_processor
docker build -t dataalchemy-operator:latest ./deploy/operator

# Import images to k3d cluster if k3d is available
if command -v k3d &> /dev/null && k3d cluster list 2>/dev/null | grep -q "dataalchemy"; then
    echo -e "${CYAN}📦 Importing images to k3d cluster...${NC}"
    k3d image import data-processor:latest -c dataalchemy 2>/dev/null || echo "⚠️  Failed to import data-processor image"
    k3d image import dataalchemy-operator:latest -c dataalchemy 2>/dev/null || echo "⚠️  Failed to import operator image"
    echo -e "${GREEN}✅ Images imported to k3d cluster${NC}"
    
    # Wait a moment for cluster to stabilize after image import
    echo -e "${CYAN}⏳ Waiting for cluster to stabilize...${NC}"
    sleep 2
    
    # Re-verify cluster connection after image import
    if ! kubectl cluster-info &>/dev/null; then
        echo -e "${YELLOW}⚠️  Cluster connection issue after image import, retrying...${NC}"
        export KUBECONFIG=$(k3d kubeconfig write dataalchemy)
        sleep 2
        if ! kubectl cluster-info &>/dev/null; then
            echo -e "${RED}❌ Failed to connect to cluster after image import${NC}"
            exit 1
        fi
    fi
    echo -e "${GREEN}✅ Cluster connection verified${NC}"
fi

# 2. Apply Kubernetes Infrastructure (CRDs, RBAC, Secrets, Operator)
echo -e "\n${YELLOW}[2/3] Applying K8s Manifests (Infrastructure & Secrets)...${NC}"

# Create Secret from .env
if [ -f ".env" ]; then
    echo -e "${CYAN}🔐 Creating secret 'dataalchemy-secret' from .env...${NC}"
    
    # Parse .env file and extract AWS credentials
    SECRET_ARGS=()
    while IFS='=' read -r key value || [ -n "$key" ]; do
        # Skip comments and empty lines
        [[ "$key" =~ ^#.*$ ]] && continue
        [[ -z "$key" ]] && continue
        
        # Remove quotes if present
        value=$(echo "$value" | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
        
        if [[ "$key" == "AWS_ACCESS_KEY_ID" ]] || [[ "$key" == "AWS_SECRET_ACCESS_KEY" ]]; then
            SECRET_ARGS+=("--from-literal=$key=$value")
        fi
    done < .env
    
    if [ ${#SECRET_ARGS[@]} -gt 0 ]; then
        kubectl delete secret dataalchemy-secret --ignore-not-found=true || true
        kubectl create secret generic dataalchemy-secret "${SECRET_ARGS[@]}"
    fi
else
    echo -e "${YELLOW}⚠️  .env file not found. Secret creation skipped.${NC}"
fi

# Verify kubectl is working before applying manifests
if ! kubectl cluster-info &>/dev/null; then
    echo -e "${RED}❌ Cannot connect to Kubernetes cluster. Please check:${NC}"
    echo "   1. Cluster is running: k3d cluster list"
    echo "   2. Kubeconfig is set: echo \$KUBECONFIG"
    exit 1
fi

kubectl apply -f deploy/operator/manifests.yaml

# 3. Deploy Stack
echo -e "\n${YELLOW}[3/3] Deploying DataAlchemy Stack...${NC}"

# Check if k3d cluster exists, if so use k3d config
STACK_CONFIG="deploy/examples/stack-instance.yaml"
if k3d cluster list 2>/dev/null | grep -q "dataalchemy"; then
    echo -e "${CYAN}Detected k3d cluster, using k3d configuration...${NC}"
    STACK_CONFIG="deploy/examples/stack-instance-k3d.yaml"
fi

kubectl apply -f "$STACK_CONFIG"

echo -e "\n${GREEN}✅ Deployment Complete!${NC}"

echo -e "\n${YELLOW}[Service Info]${NC}"
if command -v kubectl &> /dev/null; then
    # Check service type and provide appropriate info
    SERVICE_TYPE=$(kubectl get svc -l stack=dataalchemy -o jsonpath='{.items[0].spec.type}' 2>/dev/null || echo "LoadBalancer")
    
    if [[ "$SERVICE_TYPE" == "NodePort" ]]; then
        echo -e "${CYAN}Services are exposed via NodePort (k3d/k8s):${NC}"
        echo "Check service ports with: kubectl get svc -l stack=dataalchemy"
        echo "For k3d, ports are mapped to host. Check k3d cluster configuration."
    else
        echo -e "${CYAN}Services are exposed via LoadBalancer:${NC}"
        echo "MinIO API: http://localhost:9000"
        echo "MinIO Console: http://localhost:9001"
        echo "Redis: localhost:6379"
    fi
else
    echo "Redis and MinIO services deployed. Check with: kubectl get svc"
fi

echo -e "\n${YELLOW}[Data Sync]${NC}"
echo -e "${CYAN}If this is a fresh install, initialize MinIO data:${NC}"
echo "   uv run python scripts/manage_minio.py upload"

echo -e "\nCheck status with: ${CYAN}kubectl get das,pods,jobs${NC}"
