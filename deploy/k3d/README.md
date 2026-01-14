# DataAlchemy K3d Deployment Guide

This guide explains how to deploy the complete DataAlchemy stack to a K3d (K8s) cluster.

## Architecture Overview

All components run inside K3d:
- **MinIO**: S3-compatible storage (exposed via Ingress at `minio.localhost`)
- **Redis**: Internal cache (ClusterIP only)
- **WebUI**: FastAPI interface (exposed via Ingress at `data-alchemy.localhost`)
- **Coordinator**: Runs Agent S scheduler for automated pipelines
- **Training Jobs**: On-demand LoRA training

4. **AMD GPU Support (ROCm)**:
   - Ensure ROCm 7.1 is installed on the host.
   - The cluster uses `hostPath` mounts for `/dev/kfd` and `/dev/dri` to enable GPU acceleration in PyTorch.
   - Create the cluster (defaults to `dataalchemy` in setup scripts):
     ```bash
     ./scripts/setup/setup_k3d.sh
     ```

## Quick Start

### 1. Deploy the Stack

Run the deployment script:
```bash
./scripts/k3d-deploy.sh
```

This will:
1. Build Docker images (WebUI and Worker)
2. Import images into K3d
3. Apply all Kubernetes manifests
4. Wait for deployments to be ready
5. Display access URLs

### 2. Configure /etc/hosts (Recommended)

To access services via domain names, map the K3d LoadBalancer internal IP (usually `172.x.x.x`). 
You can find the IP with: `kubectl get svc -n kube-system traefik`.

Add these entries to `/etc/hosts`:
```
172.19.0.2 data-alchemy.localhost minio.localhost minio-console.localhost
```

*Note: The smart management script `manage_minio.py` can automatically discover this IP if DNS is not configured.*

### 3. Configure Secrets

Edit `deploy/k3d/05-webui.yaml` to add your API keys:
```yaml
stringData:
  DEEPSEEK_API_KEY: "your-api-key-here"
  DEEPSEEK_BASE_URL: "https://api.deepseek.com"
```

Then reapply:
```bash
kubectl apply -f deploy/k3d/05-webui.yaml
kubectl rollout restart deployment/webui -n data-alchemy
```

Upload files to MinIO. The script is **VPN/Proxy aware** and uses smart IP discovery for K3d:
```bash
# Upload local data/raw to MinIO (s3://lora-data/raw)
uv run python scripts/ops/manage_minio.py upload --path data/raw
```

**Note**: Set the environment variable for Ingress:
```bash
export S3_ENDPOINT=http://minio.localhost
```

### 5. Access the WebUI

Open your browser:
```
http://data-alchemy.localhost
```

## Component Details

### MinIO (S3 Storage)
- **API**: `http://minio.localhost`
- **Console**: `http://minio-console.localhost`
- **Credentials**: admin / minioadmin

### WebUI
- **URL**: `http://data-alchemy.localhost`
- **Features**: Chat interface, session management, real-time streaming

### Coordinator
- Runs Agent S scheduler (24-hour interval by default)
- Automatically processes new data in MinIO
- Connects to MinIO and Redis via internal K8s DNS

## Manual Operations

### Trigger Cluster-Native ETL (Ingestion)

The DataAlchemy Operator manages Spark jobs. You can trigger a new ingestion cycle via a patch:
```bash
kubectl patch das dataalchemy -n data-alchemy --type merge -p '{"metadata": {"annotations": {"dataalchemy.io/request-ingest": "now"}}}'
```

### Run LoRA Training Job

```bash
kubectl apply -f deploy/k3d/07-lora-job.yaml
```

Monitor progress:
```bash
kubectl logs -f job/lora-training -n data-alchemy
```

### View Logs

```bash
# WebUI logs
kubectl logs -f deployment/webui -n data-alchemy

# Coordinator logs
kubectl logs -f deployment/coordinator -n data-alchemy

# MinIO logs
kubectl logs -f deployment/minio -n data-alchemy
```

### Scale Components

```bash
# Scale WebUI
kubectl scale deployment/webui --replicas=2 -n data-alchemy
```

## Troubleshooting

### Pods not starting
```bash
kubectl get pods -n data-alchemy
kubectl describe pod <pod-name> -n data-alchemy
```

### Ingress not working
1. Verify Traefik is running (K3d default):
   ```bash
   kubectl get pods -n kube-system | grep traefik
   ```

2. Check Ingress resources:
   ```bash
   kubectl get ingress -n data-alchemy
   ```

3. Ensure `/etc/hosts` is configured correctly

### MinIO connection issues
```bash
# Test from within cluster
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl http://minio.data-alchemy.svc:9000

# Test from host
curl http://minio.localhost
```

## Cleanup

Remove the entire stack:
```bash
kubectl delete namespace data-alchemy
```

Or delete the K3d cluster:
```bash
k3d cluster delete k3s-default
```

## Next Steps

- Configure GPU resources in `06-coordinator.yaml` and `07-lora-job.yaml`
- Set up persistent volumes for production
- Configure TLS for Ingress
- Integrate with CI/CD pipelines
