param (
    [switch]$Cleanup
)

$ErrorActionPreference = "Stop"

if ($Cleanup) {
    Write-Host "🧹 Cleaning up DataAlchemy environment..." -ForegroundColor Magenta
    
    # 1. Delete the Custom Resource (this should trigger operator cleanup if implemented, 
    # but we'll be thorough and delete everything)
    kubectl delete das --all --ignore-not-found=true
    
    # 2. Delete Operator and RBAC
    kubectl delete deployment dataalchemy-operator --ignore-not-found=true
    kubectl delete sa dataalchemy-operator-sa --ignore-not-found=true
    kubectl delete clusterrole dataalchemy-operator-role --ignore-not-found=true
    kubectl delete clusterrolebinding dataalchemy-operator-binding --ignore-not-found=true
    kubectl delete clusterrole spark-role --ignore-not-found=true
    kubectl delete clusterrolebinding spark-role-binding --ignore-not-found=true
    kubectl delete sa spark --ignore-not-found=true
    
    # 3. Delete CRD
    kubectl delete -f k8s/dataalchemy-crd.yaml --ignore-not-found=true
    
    # 4. Optional: Clean up leftover pods/services with specific labels
    kubectl delete pods,svc,jobs -l "dataalchemy.io/managed=true" --ignore-not-found=true
    kubectl delete pods,svc,jobs -l "stack=dataalchemy" --ignore-not-found=true
    
    Write-Host "✨ Cleanup complete!" -ForegroundColor Green
    exit
}

Write-Host "🚀 Starting DataAlchemy Operator Deployment..." -ForegroundColor Cyan

# 1. Build and Load Docker Images
Write-Host "`n[1/4] Building DataProcessor and Operator images..." -ForegroundColor Yellow
docker build -t data-processor:latest ./data_processor
docker build -t dataalchemy-operator:latest ./operator

# 2. Apply Kubernetes CRDs and RBAC
Write-Host "`n[2/4] Applying K8s Manifests (CRDs, RBAC, Secrets)..." -ForegroundColor Yellow

# Create Secret from .env
if (Test-Path ".env") {
    Write-Host "🔐 Creating secret 'dataalchemy-secret' from .env..." -ForegroundColor Gray
    $envVars = Get-Content ".env" | Where-Object { $_ -match "=" -and $_ -notmatch "^#" }
    $secretArgs = @()
    foreach ($line in $envVars) {
        $parts = $line.Split("=", 2)
        $key = $parts[0].Trim()
        $val = $parts[1].Trim()
        if ($key -eq "AWS_ACCESS_KEY_ID" -or $key -eq "AWS_SECRET_ACCESS_KEY") {
            $secretArgs += "--from-literal=$key=$val"
        }
    }
    if ($secretArgs.Count -gt 0) {
        kubectl delete secret dataalchemy-secret --ignore-not-found=true
        kubectl create secret generic dataalchemy-secret @secretArgs
    }
} else {
    Write-Warning ".env file not found. Secret creation skipped."
}

kubectl apply -f k8s/dataalchemy-crd.yaml
kubectl apply -f k8s/operator-rbac.yaml
kubectl apply -f k8s/spark-rbac.yaml

# 3. Deploy the Operator
Write-Host "`n[3/4] Deploying DataAlchemy Operator..." -ForegroundColor Yellow
kubectl delete deployment dataalchemy-operator --ignore-not-found=true
# We'll use a simple deployment for the operator itself
$operatorManifest = @"
apiVersion: apps/v1
kind: Deployment
metadata:
  name: dataalchemy-operator
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: dataalchemy-operator
  template:
    metadata:
      labels:
        app: dataalchemy-operator
    spec:
      serviceAccountName: dataalchemy-operator-sa
      containers:
      - name: operator
        image: dataalchemy-operator:latest
        imagePullPolicy: Never
        volumeMounts:
        - name: log-volume
          mountPath: /app/data/logs
      volumes:
      - name: log-volume
        hostPath:
          path: "/run/desktop/mnt/host/c/Users/Administrator/work/LoRA/data/logs"
          type: DirectoryOrCreate
"@
$operatorManifest | kubectl apply -f -

# 4. Deploy Stack
Write-Host "`n[4/4] Deploying DataAlchemy Stack..." -ForegroundColor Yellow
kubectl apply -f k8s/dataalchemy-stack.yaml

Write-Host "`n✅ Deployment Complete!" -ForegroundColor Green

Write-Host "`n[Service Info]" -ForegroundColor Yellow
Write-Host "Redis and MinIO are exposed via LoadBalancer mapping to localhost." -ForegroundColor Cyan
Write-Host "MinIO API: http://localhost:9000"
Write-Host "MinIO Console: http://localhost:9001"
Write-Host "Redis: localhost:6379"

Write-Host "`n[Data Sync]" -ForegroundColor Yellow
Write-Host "If this is a fresh install, initialize MinIO data:" -ForegroundColor Cyan
Write-Host "   uv run python scripts/manage_minio.py upload"

Write-Host "`nCheck status with: kubectl get das,pods,jobs" -ForegroundColor Cyan

