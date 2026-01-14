# Data Alchemy: Enterprise RAG + LoRA Multi-Agent System

This project is an enterprise-level artificial intelligence system that forms a **closed data loop**: internal enterprise data (Jira, Git PRs, documents, database content, backup data, etc.), data cleaning (Spark), model fine-tuning (LoRA), augmented retrieval (RAG), joint inference, and data feedback to implement AI Auto-Evolution.

The main branch is moved to linux platform.

## 📚 Architecture
![DataAlchemy](https://github.com/user-attachments/assets/e20fdd5f-9329-4988-8c67-fa77a69f1caa)

### Cloud-Native Hybrid Stack
This project uses a **Kubernetes Operator** to manage the lifecycle of core infrastructure in **K3d**.
- **Operator (Kopf)**: Manages the `DataAlchemyStack` Custom Resource.
- **Infrastructure**: Automates the deployment of **MinIO** (S3) and **Redis** (Cache).
- **Networking**: Services are exposed via **Traefik Ingress** on `minio.localhost` and `data-alchemy.localhost`.
- **Persistence**: Data is persisted to the host via K3d volume mounts.

## 🚀 Key Features

-   **Kubernetes Operator**: One-click deployment and management of the entire backend stack.
-   **Multi-Agent Architecture**:
    -   **Agent A (Cleaner)**: Triggers distributed Spark jobs via K8s Operator.
    -   **Numerical Quant Stack (New)**:
        -   **Scout**: Lightweight schema inference and data scouting.
        -   **QuantAgent**: High-dimensional feature generation (Polynomial/Interaction) using Polars Streaming.
        -   **Validator**: Metadata-driven data integrity and drift validation.
        -   **Curator**: Feature selection using chunked correlation and sparse matrix optimization.
    -   **Agent B (Trainer)**: Specialized LoRA domain training.
    -   **Agent C (Knowledge)**: FAISS-powered high-speed vector search with S3 sync and **Quant-enhanced reranking**.
    -   **Agent D (Finalist)**: Intelligent fusion of RAG facts and LoRA intuition.
    -   **Agent S (Scheduler)**: Automates periodic ingestion and training.
-   **Optimized Inference Engine**:
    -   **AMD GPU Acceleration**: Leverages `torch.compile` (Inductor) and FP16 mixed-precision for ROCm.
    -   **Dynamic Batching**: High-throughput inference with `BatchInferenceEngine`.
    -   **Intelligent Caching**: Redis-backed persistence with **Semantic Search** (sentence-transformers).
-   **Distributed RAG (Agent C)**:
    -   **S3 Persistence**: FAISS index and metadata are stored in MinIO/S3 for cross-instance sharing.
    -   **SQLite Metadata**: Replaced memory-heavy pickle files with SQLite for million-scale scalability.
    -   **Hot Reloading**: Background sync thread updates the local knowledge base from S3 without service interruption.
-   **Multi-User Auth & Session Management**:
    -   **JWT/OAuth2**: Secure token-based authentication with `pbkdf2_sha256` hashing.
    -   **Redis Session & History**: Persistent chat history and user sessions stored in Redis.
    -   **Protected WebSockets**: Real-time communication secured with JWT validation.
-   **Monitoring & Observability**:
    -   **Prometheus Metrics**: Real-time tracking of latency, throughput, batch sizes, and cache hits.
    -   **Benchmarking Tool**: Automated suite to measure P95 latency and req/s under concurrent load.
-   **Async WebUI**: Real-time WebSocket streaming for status updates and progressive response rendering.
-   **Cloud-Native ETL**: Uses Spark on Kubernetes for distributed rough cleaning and LLMs for refinement.

---

## 🛠️ Getting Started

### 1. Prerequisites
-   **AMD GPU**: Compatible with ROCm.
-   **Docker**: Ensure `docker` service is running.
-   **k3d**: Lightweight k3s cluster.
-   **uv**: [Install uv](https://github.com/astral-sh/uv).

### 2. Environment Setup

**1. One-Click Cluster Setup (Linux):**
This script creates a K3d cluster, builds Docker images, and deploys the entire stack.
```bash
# Set up k3d and deploy everything
./scripts/setup/setup_k3d.sh
./scripts/k3d-deploy.sh
```

**2. Python Environment:**
```bash
uv sync
```

**3. Initialize Data:**
Before running processing commands, upload your raw data to MinIO via the smart management script:
```bash
# Upload local data/raw to MinIO (s3://lora-data/raw)
# Handles VPN/Proxy and Ingress automatically
uv run python scripts/ops/manage_minio.py upload
```

### 3. Accessing the System

Once deployed, the services are exposed via **Traefik Ingress**. On Linux systems, we recommend using the `.test` TLD to avoid conflicts with system-level `.localhost` resolution.

| Service | Access URL | Default Credentials |
| :--- | :--- | :--- |
| **WebUI** | [http://data-alchemy.test](http://data-alchemy.test) | `admin` / `admin123` |
| **MinIO Console** | [http://minio-console.test](http://minio-console.test) | `admin` / `minioadmin` |
| **MinIO API** | `http://minio.test` | `admin` / `minioadmin` |

#### 🌐 Networking Setup (DNS)
For the domain names to work, add the following entry to your `/etc/hosts` file (replace `<LB_IP>` with your K3d LoadBalancer IP, usually `172.19.0.2`):
```bash
# Get the IP: kubectl get svc -n kube-system traefik
# Add to /etc/hosts:
<LB_IP> data-alchemy.test minio.test minio-console.test
```

> [!NOTE]
> **Why .test?** Modern Linux distributions often force `.localhost` to `127.0.0.1`. Using `.test` ensures your browser respects the `/etc/hosts` mapping to the cluster gateway.

---

### 4. Model Configuration (Pluggable)

The system uses `models.yaml` in the root directory to manage the four core models. This allows you to swap models without changing code.

#### `models.yaml` Structure:
- **Model A (Refiner)**: Converts rough data to SFT pairs (e.g., DeepSeek).
- **Model B (Embedding)**: Handles tokenization and vector embeddings (e.g., BGE).
- **Model C (Base)**: The foundation for LoRA fine-tuning (e.g., TinyLlama).
- **Model D (Finalist)**: Fuses RAG facts and LoRA intuition into final answers.

#### Example Configuration:
```yaml
model_a:
  model_id: "deepseek-chat"
  base_url: "${DEEPSEEK_BASE_URL}"
  api_key: "${DEEPSEEK_API_KEY}"

model_b:
  model_id: "BAAI/bge-small-zh-v1.5"
  device: "auto"

model_c:
  model_id: "TinyLlama/TinyLlama-1.1B-intermediate-step-1431k-3T"
  lora:
    r: 16
    alpha: 32

model_d:
  model_id: "deepseek-chat"
  temperature: 0.3
```

> [!TIP]
> **Environment Variables**: Use `${VAR_NAME}` in `models.yaml` to securely reference keys from your `.env` file.

### 4. S3/MinIO & Redis Setup

The system uses a **Kubernetes Operator** to manage MinIO and Redis. They are exposed via `LoadBalancer` to `localhost`, eliminating the need for manual port-forwarding.

Ensure your `.env` file matches the Ingress endpoints:

```env
# S3 / MinIO Configuration (K3d Ingress)
S3_ENDPOINT=http://minio.localhost
S3_BUCKET=lora-data
AWS_ACCESS_KEY_ID=admin
AWS_SECRET_ACCESS_KEY=minioadmin
```

> [!NOTE]
> **Persistence**: The Operator maps `data/minio_data` and `data/redis_data` from your project root to the containers, so your data persists even if the cluster is reset.

### 5. Running the Pipeline

The system uses **Spark in Kubernetes** for heavy data cleaning and chunking. This distributed mode is ideal for large datasets and is configured to run with multiple executor pods.

#### Execution in Cluster
The system is designed to run its core agents inside the K3d cluster.

**1. Trigger Data Ingestion (Spark ETL):**
```bash
# Manual trigger via Operator patch
kubectl patch das dataalchemy -n data-alchemy --type merge -p '{"metadata": {"annotations": {"dataalchemy.io/request-ingest": "now"}}}'
```

**2. Run Training Job:**
```bash
# Deploy a specialized LoRA training job
kubectl apply -f deploy/k3d/07-lora-job.yaml
```

**3. Managed Scheduler (Agent S):**
The `coordinator` pod runs Agent S, which periodically orchestrates the full cycle.
```bash
# View coordinator logs
kubectl logs -l app=coordinator -n data-alchemy -f
```

---

## 🏗️ Project Structure

```
.
├── src/                        # Unified AI Stack (Linux)
│   ├── agents/                 # Specialized Agents (A, B, C, D, S)
│   ├── etl/                    # Unified Spark ETL (previously data_processor)
│   ├── rag/                    # Vector Database logic
│   ├── synthesis/              # AI SFT Refinement
│   └── run_agents.py           # Unified entry point
├── deploy/                     # Kubernetes Manifests
│   ├── k3d/                    # Cluster-specific configs
│   └── operator/               # DataAlchemy Operator logic
├── scripts/                    # Automation & Ops
│   ├── setup/                  # Cluster & Image bootstrap
│   └── ops/                    # MinIO, Benchmark, Cluster management
├── data/                       # Shared Data Storage (Volumes)
├── .env                        # Local management configs
└── pyproject.toml              # Unified dependency management
```

## 🔧 Troubleshooting

-   **API Keys**: Ensure `DEEPSEEK_API_KEY` is set in `.env`.
-   **S3/Redis Connection**: If you see connection errors, ensure the Operator is running and the `DataAlchemyStack` is deployed (`kubectl get das`).
-   **K3d Networking**: If `minio.localhost` is unreachable, check `kubectl get ingress -n data-alchemy`.
