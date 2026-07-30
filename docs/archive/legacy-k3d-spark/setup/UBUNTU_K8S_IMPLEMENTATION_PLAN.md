# Ubuntu & Kubernetes/k3d Implementation Plan

## Executive Summary

This document outlines the implementation plan to make DataAlchemy work on:
1. **Ubuntu** (native Linux environment)
2. **Kubernetes/k3d** (cloud-native deployment)

The current system is optimized for Windows + Docker Desktop with WSL integration. This plan addresses platform-specific code, path handling, and deployment mechanisms.

---

## 1. Current State Analysis

### 1.1 Windows-Specific Components Identified

| Component | Location | Issue | Impact |
|-----------|----------|-------|--------|
| **PowerShell Script** | `scripts/setup_operator.ps1` | Windows-only deployment script | High - Blocks Ubuntu deployment |
| **hostPath Volumes** | `deploy/operator/templates.yaml` | Hardcoded Docker Desktop Windows paths | High - Data persistence fails on Ubuntu/k3d |
| **hostPath Volumes** | `deploy/operator/manifests.yaml` | Hardcoded Windows log path | Medium - Logs won't persist |
| **Asyncio Policy** | `src/run_agents.py` | Windows-specific event loop (already handled) | Low - Already platform-aware |
| **ROCm Dependencies** | `pyproject.toml` | Windows-only ROCm wheels | Medium - Need Linux ROCm support |
| **LoadBalancer** | `templates.yaml` | Docker Desktop auto-maps to localhost | Medium - k3d needs different approach |

### 1.2 Architecture Assumptions

- **Current**: Windows host → Docker Desktop K8s → Services via LoadBalancer → localhost
- **Target**: Ubuntu host → k3d/k8s → Services via NodePort/Ingress → localhost or cluster IP

---

## 2. Implementation Phases

### Phase 1: Ubuntu Native Support (Week 1)

#### 2.1.1 Replace PowerShell Script with Bash Equivalent
**Priority**: High  
**Effort**: 2-3 hours

**Tasks**:
- [ ] Create `scripts/setup_operator.sh` (bash equivalent)
- [ ] Support both `.env` file parsing and environment variable injection
- [ ] Add platform detection (Windows vs Linux)
- [ ] Update README with Ubuntu instructions

**Files to Create/Modify**:
- `scripts/setup_operator.sh` (new)
- `README.md` (update deployment section)

**Key Features**:
```bash
#!/bin/bash
# Cross-platform operator deployment
# Detects platform and adjusts paths accordingly
```

#### 2.1.2 Fix Platform-Specific Code Paths
**Priority**: High  
**Effort**: 1-2 hours

**Tasks**:
- [ ] Make `src/run_agents.py` asyncio policy conditional (already done, verify)
- [ ] Ensure `os.path.join()` is used consistently (already done)
- [ ] Add platform detection utility if needed

**Files to Modify**:
- `src/run_agents.py` (verify platform checks)
- `src/config.py` (verify path handling)

#### 2.1.3 ROCm Support for Ubuntu
**Priority**: Medium  
**Effort**: 3-4 hours

**Tasks**:
- [ ] Update `pyproject.toml` to support Linux ROCm
- [ ] Add conditional ROCm installation for Linux
- [ ] Document ROCm installation requirements for Ubuntu
- [ ] Test GPU detection on Ubuntu

**Files to Modify**:
- `pyproject.toml` (add Linux ROCm sources)
- `README.md` (add Ubuntu ROCm setup instructions)

**ROCm Configuration**:
```toml
# For Linux (Ubuntu)
rocm = { url = "...", marker = "sys_platform == 'linux'" }
torch = [
  { url = "...", marker = "sys_platform == 'win32'" },
  { url = "...", marker = "sys_platform == 'linux'" },  # Linux ROCm
  { index = "pytorch", marker = "sys_platform != 'win32' and sys_platform != 'linux'" }
]
```

---

### Phase 2: Kubernetes/k3d Compatibility (Week 1-2)

#### 2.2.1 Dynamic hostPath Resolution
**Priority**: High  
**Effort**: 4-5 hours

**Problem**: Hardcoded Windows paths like `/run/desktop/mnt/host/c/Users/Administrator/work/LoRA/data` won't work on Ubuntu/k3d.

**Solution**: Use environment variables or ConfigMap to inject base paths.

**Tasks**:
- [ ] Create `deploy/operator/config.yaml` for environment-specific paths
- [ ] Modify `deploy/operator/main.py` to read base path from ConfigMap or env
- [ ] Update `templates.yaml` to use dynamic path variables
- [ ] Add fallback to relative paths for k3d

**Files to Create/Modify**:
- `deploy/operator/config.yaml` (new - optional ConfigMap)
- `deploy/operator/main.py` (add path resolution logic)
- `deploy/operator/templates.yaml` (use `{{DATA_PATH}}` variable)
- `deploy/operator/manifests.yaml` (use dynamic paths)

**Implementation Approach**:
```python
# In main.py
def get_data_path():
    """Resolve data path based on platform and environment."""
    # 1. Check environment variable
    if os.getenv("DATAALCHEMY_DATA_PATH"):
        return os.getenv("DATAALCHEMY_DATA_PATH")
    
    # 2. Check ConfigMap
    # ... (k8s ConfigMap lookup)
    
    # 3. Platform-specific defaults
    if sys.platform == 'win32':
        return "/run/desktop/mnt/host/c/Users/Administrator/work/LoRA/data"
    else:
        # Ubuntu/k3d: use relative path or /data
        return os.getenv("HOME", "/tmp") + "/work/DataAlchemy/data"
```

**Template Update**:
```yaml
# templates.yaml
volumes:
  - name: redis-storage
    hostPath:
      path: "{{DATA_PATH}}/redis_data"  # Dynamic
      type: DirectoryOrCreate
```

#### 2.2.2 Service Exposure Strategy
**Priority**: Medium  
**Effort**: 2-3 hours

**Problem**: Docker Desktop auto-maps LoadBalancer to localhost, but k3d/k8s needs explicit configuration.

**Solution**: Support multiple service types with platform detection.

**Tasks**:
- [ ] Add `SERVICE_TYPE` variable to operator (LoadBalancer vs NodePort)
- [ ] For k3d: Use NodePort or Ingress
- [ ] For k8s: Support LoadBalancer (cloud) or NodePort (bare metal)
- [ ] Update templates to conditionally use service type

**Files to Modify**:
- `deploy/operator/templates.yaml` (add `{{SERVICE_TYPE}}` variable)
- `deploy/operator/main.py` (detect and set service type)
- `deploy/examples/stack-instance.yaml` (add serviceType spec)

**Service Type Logic**:
```python
def get_service_type():
    """Determine service type based on environment."""
    # Check if running in k3d (lightweight)
    if os.getenv("K3D_CLUSTER"):
        return "NodePort"
    
    # Check if LoadBalancer is available (cloud)
    # Default to NodePort for bare metal
    return os.getenv("SERVICE_TYPE", "NodePort")
```

#### 2.2.3 Persistent Volume Strategy
**Priority**: High  
**Effort**: 3-4 hours

**Problem**: `hostPath` works for single-node but not ideal for multi-node clusters.

**Solution**: Support both `hostPath` (dev/single-node) and `PersistentVolumeClaim` (production).

**Tasks**:
- [ ] Add PVC templates for production use
- [ ] Keep hostPath as default for development
- [ ] Add storage class configuration
- [ ] Document storage options in README

**Files to Create/Modify**:
- `deploy/operator/templates.yaml` (add PVC option)
- `deploy/examples/stack-instance-prod.yaml` (new - production example)
- `README.md` (document storage options)

**Storage Strategy**:
```yaml
# Development (hostPath)
volumes:
  - name: redis-storage
    hostPath:
      path: "{{DATA_PATH}}/redis_data"

# Production (PVC)
volumes:
  - name: redis-storage
    persistentVolumeClaim:
      claimName: "{{NAME}}-redis-pvc"
```

---

### Phase 3: Testing & Documentation (Week 2)

#### 2.3.1 Cross-Platform Testing
**Priority**: High  
**Effort**: 4-6 hours

**Tasks**:
- [ ] Test on Ubuntu 22.04/24.04
- [ ] Test with k3d cluster
- [ ] Test with minikube (alternative)
- [ ] Test with cloud k8s (GKE/EKS/AKS) - optional
- [ ] Verify data persistence across restarts
- [ ] Verify service connectivity

**Test Scenarios**:
1. Fresh Ubuntu install → k3d setup → operator deployment
2. Data ingestion pipeline end-to-end
3. Service connectivity (MinIO, Redis)
4. Data persistence after pod restarts
5. Multi-node k3d cluster (if applicable)

#### 2.3.2 Documentation Updates
**Priority**: Medium  
**Effort**: 2-3 hours

**Tasks**:
- [ ] Update README with Ubuntu setup instructions
- [ ] Add k3d/k8s deployment guide
- [ ] Document environment variables
- [ ] Add troubleshooting section for Ubuntu/k3d
- [ ] Update architecture diagram if needed

**Files to Modify**:
- `README.md` (add Ubuntu/k3d sections)
- `docs/ARCHITECTURE.md` (update hybrid architecture section)
- `docs/UBUNTU_K8S_IMPLEMENTATION_PLAN.md` (this file - mark as complete)

---

## 3. Detailed Implementation Steps

### Step 1: Create Bash Deployment Script

**File**: `scripts/setup_operator.sh`

```bash
#!/bin/bash
set -e

# Cross-platform operator deployment script
# Supports both Windows (WSL) and Ubuntu

PLATFORM=$(uname -s)
echo "🚀 Detected platform: $PLATFORM"

# Build images
echo "[1/3] Building Docker images..."
docker build -t data-processor:latest ./data_processor
docker build -t dataalchemy-operator:latest ./deploy/operator

# Create secrets from .env
if [ -f ".env" ]; then
    echo "[2/3] Creating Kubernetes secrets from .env..."
    # Parse .env and create secret
    kubectl create secret generic dataalchemy-secret \
        --from-env-file=.env \
        --dry-run=client -o yaml | kubectl apply -f -
else
    echo "⚠️  .env file not found. Skipping secret creation."
fi

# Apply manifests
echo "[3/3] Applying Kubernetes manifests..."
kubectl apply -f deploy/operator/manifests.yaml

# Deploy stack
kubectl apply -f deploy/examples/stack-instance.yaml

echo "✅ Deployment complete!"
```

### Step 2: Update Operator for Dynamic Paths

**Modify**: `deploy/operator/main.py`

Add path resolution function:
```python
import os
import sys

def resolve_data_path(spec, namespace):
    """Resolve data path based on environment."""
    # Priority 1: Spec override
    if spec.get('storage', {}).get('dataPath'):
        return spec['storage']['dataPath']
    
    # Priority 2: Environment variable
    env_path = os.getenv("DATAALCHEMY_DATA_PATH")
    if env_path:
        return env_path
    
    # Priority 3: Platform defaults
    if sys.platform == 'win32':
        # Docker Desktop Windows path
        return "/run/desktop/mnt/host/c/Users/Administrator/work/LoRA/data"
    else:
        # Ubuntu/k3d: use project root or /data
        project_root = os.getenv("PROJECT_ROOT", "/data")
        return f"{project_root}/data"
```

### Step 3: Update Templates for Dynamic Paths

**Modify**: `deploy/operator/templates.yaml`

Replace hardcoded paths with variables:
```yaml
volumes:
  - name: redis-storage
    hostPath:
      path: "{{DATA_PATH}}/redis_data"
      type: DirectoryOrCreate
```

### Step 4: Add k3d-Specific Configuration

**Create**: `deploy/examples/stack-instance-k3d.yaml`

```yaml
apiVersion: dataalchemy.io/v1alpha1
kind: DataAlchemyStack
metadata:
  name: dataalchemy
spec:
  projectName: "DataAlchemy"
  credentialsSecret: "dataalchemy-secret"
  storage:
    replicas: 1
    dataPath: "/data"  # k3d hostPath
  cache:
    replicas: 1
  compute:
    spark:
      image: "data-processor:latest"
```

---

## 4. Environment Variables Reference

### New Variables for Ubuntu/k3d

| Variable | Description | Default | Platform |
|----------|-------------|---------|----------|
| `DATAALCHEMY_DATA_PATH` | Base path for persistent data | Platform-specific | All |
| `SERVICE_TYPE` | Kubernetes service type | `NodePort` (k3d) / `LoadBalancer` (cloud) | k8s |
| `K3D_CLUSTER` | Set to `true` if using k3d | `false` | k3d |
| `PROJECT_ROOT` | Project root directory | `$HOME/work/DataAlchemy` | Ubuntu |

---

## 5. Migration Guide

### For Existing Windows Users

1. **No changes required** - Windows setup continues to work
2. PowerShell script remains functional
3. Existing data paths are preserved

### For New Ubuntu Users

1. Install prerequisites:
   ```bash
   # Install k3d
   curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
   
   # Install kubectl
   sudo apt-get update && sudo apt-get install -y kubectl
   
   # Install Docker
   sudo apt-get install -y docker.io
   ```

2. Create k3d cluster:
   ```bash
   k3d cluster create dataalchemy \
     --port "9000:30000@loadbalancer" \
     --port "9001:30001@loadbalancer" \
     --port "6379:30002@loadbalancer"
   ```

3. Deploy operator:
   ```bash
   chmod +x scripts/setup_operator.sh
   ./scripts/setup_operator.sh
   ```

---

## 6. Risk Assessment & Mitigation

### Risks

| Risk | Impact | Probability | Mitigation |
|------|--------|--------------|------------|
| Path resolution fails | High | Medium | Comprehensive testing, fallback paths |
| Service connectivity issues | High | Low | Multiple service types, documentation |
| Data loss during migration | Critical | Low | Backup procedures, migration guide |
| ROCm compatibility | Medium | Medium | Conditional installation, CPU fallback |

### Mitigation Strategies

1. **Backward Compatibility**: All changes maintain Windows support
2. **Feature Flags**: Use environment variables for platform-specific features
3. **Testing**: Comprehensive cross-platform testing before release
4. **Documentation**: Clear migration paths and troubleshooting guides

---

## 7. Success Criteria

### Phase 1 (Ubuntu Native)
- [ ] Project runs natively on Ubuntu 22.04+
- [ ] All CLI commands work without WSL
- [ ] ROCm GPU support functional (if GPU available)
- [ ] Documentation updated

### Phase 2 (k3d/k8s)
- [ ] Operator deploys successfully on k3d
- [ ] MinIO and Redis accessible via NodePort
- [ ] Data persists across pod restarts
- [ ] Spark jobs execute successfully
- [ ] Works on standard k8s clusters (GKE/EKS/AKS)

### Phase 3 (Production Ready)
- [ ] End-to-end pipeline tested on Ubuntu + k3d
- [ ] Performance benchmarks comparable to Windows
- [ ] Migration guide completed
- [ ] All documentation updated

---

## 8. Timeline Estimate

| Phase | Duration | Dependencies |
|-------|----------|--------------|
| Phase 1: Ubuntu Native | 1 week | None |
| Phase 2: k3d/k8s | 1 week | Phase 1 |
| Phase 3: Testing & Docs | 3-5 days | Phase 1, 2 |
| **Total** | **2-3 weeks** | |

---

## 9. Next Steps

1. **Review this plan** with the team
2. **Prioritize phases** based on business needs
3. **Assign tasks** to developers
4. **Set up test environment** (Ubuntu VM or cloud instance)
5. **Begin Phase 1 implementation**

---

## 10. References

- [k3d Documentation](https://k3d.io/)
- [Kubernetes hostPath Volumes](https://kubernetes.io/docs/concepts/storage/volumes/#hostpath)
- [ROCm Installation Guide](https://rocm.docs.amd.com/)
- [Docker Desktop vs k3d Comparison](https://k3d.io/v5.4.6/usage/guides/docker-desktop/)

---

**Document Version**: 1.0  
**Last Updated**: 2025-01-XX  
**Status**: Draft - Pending Review
