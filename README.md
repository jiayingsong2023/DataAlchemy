# Data Alchemy: Enterprise RAG + LoRA Multi-Agent System

This project is an enterprise-level artificial intelligence system that forms a **closed data loop**: internal enterprise data (Jira, Git PRs, documents, database content, backup data, etc.), data cleaning (Spark), model fine-tuning (LoRA), augmented retrieval (RAG), joint inference, and data feedback to implement AI Auto-Evolution.

The main branch is optimized for **Linux (Ubuntu/Debian)** running **K3d (Kubernetes)** with **ROCm (AMD GPU)** support.

## 📚 Architecture
![DataAlchemy](https://github.com/user-attachments/assets/e20fdd5f-9329-4988-8c67-fa77a69f1caa)

### Cloud-Native Hybrid Stack
This project uses a **Kubernetes Operator** to manage the lifecycle of core infrastructure in **K3d**.
- **Operator (Kopf)**: Manages the `DataAlchemyStack` Custom Resource.
- **Unified S3 Storage**: All data (raw, processed, SFT pairs) and models (base models, adapters) are stored in **MinIO**.
- **Offline-First Design**: Fully supports air-gapped environments by loading models from local paths with `TRANSFORMERS_OFFLINE=1`.
- **Infrastructure**: Automates the deployment of **MinIO** (S3) and **Redis** (Cache).
- **Networking**: Services are exposed via **Traefik Ingress** on `minio.test` and `data-alchemy.test`.
- **Direct Persistence**: Uses `hostPath` mapping to bind the host's `./data` directory directly into K3d nodes at `/data`.

## 🚀 Key Features

-   **Kubernetes Operator**: One-click deployment and management of the entire backend stack.
-   **Multi-Agent Architecture**:
    -   **Agent A (Cleaner)**: Triggers distributed Spark jobs via K8s Operator.
    -   **Agent B (Trainer)**: Specialized LoRA domain training with S3 streaming datasets.
    -   **Agent C (Knowledge)**: FAISS-powered high-speed vector search with S3 sync and BGE-Reranker support.
    -   **Agent D (Finalist)**: Intelligent fusion of RAG facts and LoRA intuition.
    -   **Agent S (Scheduler)**: Automates periodic ingestion and training.
-   **Optimized Inference Engine**:
    -   **AMD GPU Acceleration**: Leverages `torch.compile` (Inductor) and FP16 mixed-precision for ROCm.
    -   **Dynamic Batching & Semantic Cache**: High-throughput inference with `BatchInferenceEngine` and Redis-backed semantic caching.
-   **Fully Automated Lifecycle**:
    -   **Full-Cycle Job**: A single command triggers the entire pipe: Clean -> Synthesize -> Index -> Train -> Sync.

---

## 🛠️ Getting Started

### 1. Prerequisites
-   **OS**: Linux (Ubuntu 20.04/22.04 recommended)
-   **AMD GPU**: Compatible with ROCm driver installed.
-   **Docker**: Ensure `docker` service is running.
-   **k3d**: Lightweight k3s cluster.
-   **uv**: Python package manager.

### 2. Environment & Cluster Setup

**1. Prepare Models (Local):**
Download the required models into your local `data/models` directory:
- `BAAI/bge-small-zh-v1.5` (Embedding)
- `BAAI/bge-reranker-base` (Reranker)
- `TinyLlama/TinyLlama-1.1B-intermediate-step-1431k-3T` (Base LLM)

**2. One-Click Cluster Setup:**
```bash
# Create K3d cluster with host volume mapping
./scripts/setup/setup_k3d.sh
# Build images and deploy resources
./scripts/k3d-deploy.sh
```

**3. Configure Python Environment (Local):**
```bash
uv sync
```

**4. Networking Setup (DNS):**
Add the following to your `/etc/hosts` file (replace IP with `docker inspect k3d-dataalchemy-serverlb`):
```bash
# Example LoadBalancer IP: 172.18.0.3
172.18.0.3 data-alchemy.test minio.test minio-console.test
```

### 3. Accessing the System

| Service | Access URL | Default Credentials |
| :--- | :--- | :--- |
| **WebUI** | [http://data-alchemy.test](http://data-alchemy.test) | `admin` / `admin123` |
| **MinIO Console** | [http://minio-console.test](http://minio-console.test) | `admin` / `minioadmin` |
| **MinIO API** | `http://minio.test` | `admin` / `minioadmin` |

---

## 🔄 End-to-End Workflow

### Step 1: Upload Raw Data
```bash
uv run python scripts/ops/manage_minio.py upload
```

### Step 2: Trigger Full Auto-Evolution Cycle
Submit a single Kubernetes Job to perform cleaning, synthesis, indexing, and training:
```bash
kubectl apply -f deploy/k3d/08-full-cycle-job.yaml
```
*Monitor*: `kubectl logs -l app=lora-full-cycle -n data-alchemy -f`

### Step 3: Inference & Feedback
1.  Open [http://data-alchemy.test](http://data-alchemy.test).
2.  Ask questions. The system will use the latest fine-tuned LoRA adapter and RAG index synced from S3.
3.  Provide feedback (Good/Bad) to further improve the data loop.

---

## 🔧 Troubleshooting

-   **Offline Model Loading**:
    -   Ensure `MODEL_DIR` environment variable is set correctly in ConfigMap.
    -   Verify that `models.yaml` uses `${MODEL_DIR}/...` paths.
-   **Ingress Connection Refused**:
    -   Check if k3d cluster was created with `--port "80:80@loadbalancer"`.
-   **Spark Ingest Fails**:
    -   Ensure Hadoop AWS JARs are present in `data/spark-jars` for offline dependency loading.
