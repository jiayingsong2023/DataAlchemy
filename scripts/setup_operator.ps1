param (
    [switch]$Cleanup
)

$ErrorActionPreference = "Stop"

if ($Cleanup) {
    Write-Host "🧹 Cleaning up DataAlchemy environment..." -ForegroundColor Magenta
    
    # 1. Delete the Custom Resource
    # Note: We do this first to let K8s garbage collection start
    kubectl delete das --all --ignore-not-found=true --timeout=30s
    
    # 2. Delete Infrastructure (Operator, RBAC, CRD)
    kubectl delete -f deploy/operator/manifests.yaml --ignore-not-found=true
    
    # 3. Delete Secrets and shared SA
    kubectl delete secret dataalchemy-secret --ignore-not-found=true
    kubectl delete sa spark --ignore-not-found=true
    
    # 4. Thorough cleanup of orphaned resources (handling both old and new names)
    Write-Host "🧹 Removing any orphaned pods, services, or jobs..." -ForegroundColor Gray
    kubectl delete pods,svc,deploy,jobs,ingress -l "dataalchemy.io/managed=true" --ignore-not-found=true
    kubectl delete pods,svc,deploy,jobs,ingress -l "stack=dataalchemy" --ignore-not-found=true
    kubectl delete pods,svc,deploy,jobs,ingress -l "stack=test-stack" --ignore-not-found=true
    
    # 5. Optional: Clean up local hostPath data (PRESERVING users.db)
    $confirmation = Read-Host "`n❓ Do you also want to WIPE all ephemeral data on local disk (MinIO, Redis, RAG Index)? [y/N]"
    if ($confirmation -eq 'y') {
        Write-Host "🗑️ Wiping specific local data directories..." -ForegroundColor Red
        # Wipe MinIO and Redis hostPaths
        if (Test-Path "data/minio_data") { Remove-Item -Recurse -Force "data/minio_data/*" }
        if (Test-Path "data/redis_data") { Remove-Item -Recurse -Force "data/redis_data/*" }
        
        # Wipe RAG Index files but PRESERVE users.db
        Write-Host "🧹 Clearing RAG index files (faiss_index, metadata.db)..." -ForegroundColor Gray
        Get-ChildItem "data" -Include "faiss_index.bin", "metadata.db*" -Recurse | Remove-Item -Force
        
        Write-Host "✅ Local ephemeral data wiped. (users.db preserved)" -ForegroundColor Green
    }

    Write-Host "✨ Cleanup complete!" -ForegroundColor Green
    exit
}

Write-Host "🚀 Starting DataAlchemy Operator Deployment..." -ForegroundColor Cyan

# 1. Build and Load Docker Images
Write-Host "`n[1/3] Building DataProcessor and Operator images..." -ForegroundColor Yellow
docker build -t data-processor:latest ./data_processor
docker build -t dataalchemy-operator:latest ./deploy/operator

# 2. Apply Kubernetes Infrastructure (CRDs, RBAC, Secrets, Operator)
Write-Host "`n[2/3] Applying K8s Manifests (Infrastructure & Secrets)..." -ForegroundColor Yellow

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

kubectl apply -f deploy/operator/manifests.yaml

# 3. Deploy Stack
Write-Host "`n[3/3] Deploying DataAlchemy Stack..." -ForegroundColor Yellow
kubectl apply -f deploy/examples/stack-instance.yaml

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

