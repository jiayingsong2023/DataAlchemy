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
- **Networking**: Exposed via **Traefik Ingress** on `minio.test`, `minio-console.test`, and `data-alchemy.test`.
- **Persistent Storage**: Uses a **PersistentVolume (PV)** mapping to bind the host's `./data` directory directly to `/data` in K3d nodes.

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
-   **OS**: Linux (Ubuntu 24.04/22.04 recommended)
-   **AMD GPU**: Compatible with ROCm driver installed.
-   **Docker**: Ensure `docker` service is running.
-   **k3d**: Lightweight k3s cluster.
-   **helm**: Kubernetes package manager (v3+).
-   **uv**: Python package manager.

### 2. Environment & Cluster Setup

**1. Prepare Models (Local):**
Download the required models into your local `data/models` directory:
- `BAAI/bge-small-zh-v1.5` (Embedding)
- `BAAI/bge-reranker-base` (Reranker)
- `TinyLlama/TinyLlama-1.1B-intermediate-step-1431k-3T` (Base LLM)

**2. Configure Environment & Secrets (.env):**
Create a `.env` file in the project root and add your configuration (the deployment script will automatically use these):
```env
# DeepSeek API (required for synthesis and Agent D)
DEEPSEEK_API_KEY=sk-your-key-here
DEEPSEEK_BASE_URL=https://api.deepseek.com

# Auth Secret (recommended)
AUTH_SECRET_KEY=your-random-secret-here
```

**3. One-Click Cluster Setup:**
```bash
# Create K3d cluster with host volume mapping
./scripts/setup/setup_k3d.sh
# Build images and deploy Helm chart to 'data-alchemy' namespace
./scripts/helm-deploy.sh
```

**4. Configure Python Environment (Local):**
```bash
uv sync
```

**5. Networking Setup (DNS):**
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

Trigger the entire pipeline (Clean -> Synthesize -> Index -> Train -> Sync) via a Kubernetes Job:

```bash
# Option A: Trigger via Kubernetes Annotation (Recommended)
kubectl annotate das dataalchemy dataalchemy.io/request-full-cycle=$(date +%s) -n data-alchemy --overwrite

# Option B: Trigger via WebUI API
curl -X POST http://data-alchemy.test/api/jobs/full-cycle -H "Authorization: Bearer <token>"

# Option C: Trigger via Helm (Legacy)
helm upgrade data-alchemy deploy/charts/data-alchemy -n data-alchemy --reuse-values --set jobs.fullCycle.enabled=true
```

**What the Coordinator does:**
- **Orchestration**: Invokes `Agent_A` for Spark ETL, `Agent_C` for Vector indexing, and `Agent_B` for LoRA training.
- **Data Persistence**: Uses the host-mapped `./data` volume for models and caches, ensuring fast start-up.
- **Hot-Reload**: Once a training job uploads a new LoRA adapter to MinIO, the WebUI's Agent B detects and downloads it automatically on the next user query. You can also force a reload via API:
  ```bash
  curl -X POST http://data-alchemy.test/api/models/reload -H "Authorization: Bearer <token>"
  ```

*Monitor progress*: 
```bash
# 查看训练进度
kubectl logs -n data-alchemy -l component=lora-full-cycle -f --tail=100

# 查看 WebUI 日志
kubectl logs -n data-alchemy -l app=webui -f
```

### Step 3: Run Individual Stages (Advanced) in place of step 2
You can also run specific parts of the pipeline manually using the CLI (either locally or via `kubectl exec`):

| Stage | CLI Command | Description |
| :--- | :--- | :--- |
| **Rough Clean** | `python src/run_agents.py ingest --stage wash` | Triggers distributed Spark ETL for raw data cleaning. |
| **Refine Clean** | `python src/run_agents.py ingest --stage refine --synthesis` | LLM-based knowledge synthesis and FAISS indexing. |
| **LoRA Train** | `python src/run_agents.py train` | Fine-tunes the model using streaming datasets from MinIO. |
| **Quantization** | `python src/run_agents.py quant` | Performs feature engineering on numerical metrics. |

### Step 4: Inference & Feedback
1.  Open [http://data-alchemy.test](http://data-alchemy.test).
2.  Ask questions. The system will use the latest fine-tuned LoRA adapter and RAG index synced from S3.
3.  Provide feedback (Good/Bad) to further improve the data loop.

---

## 🔧 Troubleshooting

-   **Log Viewing (Cloud-Native)**:
    -   Logs are no longer stored in files. Use `kubectl logs` to view real-time streams.
    -   For Operator issues: `kubectl logs -n data-alchemy -l app=dataalchemy-operator`
-   **Offline Model Loading**:
    -   Ensure `MODEL_DIR` environment variable is set correctly in ConfigMap.
    -   Verify that `models.yaml` uses `${MODEL_DIR}/...` paths.
-   **Ingress Connection Refused**:
    -   Check if k3d cluster was created with `--port "80:80@loadbalancer"`.
-   **Spark Ingest Fails**:
    -   Ensure Hadoop AWS JARs are present in `data/spark-jars` for offline dependency loading.
