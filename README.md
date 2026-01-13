# Data Alchemy: Enterprise RAG + LoRA Multi-Agent System

This project is an enterprise-level artificial intelligence system that forms a **closed data loop**: internal enterprise data (Jira, Git PRs, documents, database content, backup data, etc.), data cleaning (Spark), model fine-tuning (LoRA), augmented retrieval (RAG), joint inference, and data feedback to implement AI Auto-Evolution.

The main branch is moved to linux platform.

## 📚 Architecture
![DataAlchemy](https://github.com/user-attachments/assets/e20fdd5f-9329-4988-8c67-fa77a69f1caa)

### Cloud-Native Hybrid Stack
This project uses a **Kubernetes Operator** to manage the lifecycle of core infrastructure.
- **Operator (Kopf)**: Manages the `DataAlchemyStack` Custom Resource.
- **Infrastructure**: Automates the deployment of **MinIO** (S3), **Redis** (Cache), and **Spark Jobs** (ETL).
- **Hybrid Networking**: Infrastructure runs in K8s (Docker Desktop) but is exposed to the Windows host via `LoadBalancer` on `localhost`.
- **Persistence**: Data is persisted directly to the Windows host via `hostPath` mounts.

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
-   **Cloud-Native ETL**: Uses Spark on Kubernetes for distributed rough cleaning and LLMs in Windows for refinement.

---

## 🛠️ Getting Started

### 1. Prerequisites
-   **AMD GPU**: Compatible with ROCm.
-   **Docker Desktop**: With Kubernetes enabled.
-   **k3d** (Optional): If you prefer a lightweight k3s cluster over default Docker K8s.
-   **uv**: [Install uv](https://github.com/astral-sh/uv).

### 2. Environment Setup

**1. One-Click Hybrid Deployment (Windows PowerShell):**
This script builds the necessary Docker images and deploys the Operator and Infrastructure (MinIO, Redis) to your local Kubernetes cluster.
```powershell
# Deploys Operator, MinIO, and Redis
.\scripts\setup_operator.ps1
```

**2. Python Environment (Windows - AI & Refinement):**
```powershell
uv sync
```

**3. Initialize Data:**
Before running processing commands, upload your raw data to the newly deployed MinIO:
```powershell
# Upload local data/raw to MinIO (s3://lora-data/raw)
uv run python scripts/manage_minio.py upload
```

### 3. Model Configuration (Pluggable)

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

#### Environment Configuration:

Ensure your `.env` file matches the infrastructure deployed by the Operator:

```env
# S3 / MinIO Configuration (Exposed on localhost)
S3_ENDPOINT=http://localhost:9000
S3_BUCKET=lora-data
AWS_ACCESS_KEY_ID=admin
AWS_SECRET_ACCESS_KEY=minioadmin

# Redis Configuration (Exposed on localhost)
REDIS_URL=redis://localhost:6379
```

> [!NOTE]
> **Persistence**: The Operator maps `data/minio_data` and `data/redis_data` from your project root to the containers, so your data persists even if the cluster is reset.

### 5. Running the Pipeline

The system uses **Spark in Kubernetes** for heavy data cleaning and chunking. This distributed mode is ideal for large datasets and is configured to run with multiple executor pods.

#### Step 1: Ingestion (Agent A + Quant + Agent C)
Rough cleaning (Spark) -> Feature Quant (Polars) -> Refinement (LLM) -> Indexing (FAISS).

**1. Rough Cleaning only (Washing):**
```powershell
uv run data-alchemy ingest --mode spark --stage wash
```
-   Produces `cleaned_corpus.jsonl` (for SFT), `rag_chunks.jsonl` (for RAG), and `metrics.parquet` (for Quant) in the `processed/` directory.

**2. Numerical Quant only (Feature Engineering):**
```powershell
# Refine numerical metrics from Spark into high-dimensional features
uv run data-alchemy quant --input data/processed/metrics.parquet --output data/processed/quant
```
-   Uses **Polars Streaming** to process million-row datasets with minimal memory footprint.

**3. Refinement & Indexing only:**
```powershell
# Convert rough data to SFT pairs and build knowledge index
uv run data-alchemy ingest --stage refine --synthesis --max_samples 50
```
-   Expects `cleaned_corpus.jsonl` and `rag_chunks.jsonl` to exist.

**4. Full Ingestion Pipeline (Default):**
```powershell
# Rough cleaning + Auto-Quant + LLM Synthesis + FAISS Indexing in one go
uv run data-alchemy ingest --mode spark --synthesis --max_samples 50
```
-   **Rough Cleaning**: `Agent A` produces the processed files in S3/Local.
-   **Auto-Quant**: When `--synthesis` is enabled, the system automatically runs the Quant Agent to extract numerical insights.
-   **Refinement**: `SFT Generator` converts rough data into `data/sft_train.jsonl`, incorporating numerical insights for "expert-level" training pairs.
-   **Indexing**: `Agent C` builds FAISS index from `rag_chunks.jsonl`.

#### Step 2: Training (Agent B)
Fine-tune the model using the refined SFT data.
```powershell
uv run train-lora
```

#### Step 4: Interactive Chat
Combine RAG facts and LoRA intuition for expert answers.

**1. WebUI Chat (Async & Streaming):**
```powershell
# Start the WebUI server (HTTPS on 8443)
uv run python webui/app.py
```
Then open `https://localhost:8443` in your browser.
- **Features**: Real-time status updates (Retrieving -> Consulting -> Fusing), streaming responses, and Redis-backed session persistence.

#### Step 5: Monitoring & Benchmarking

**1. View Real-time Metrics:**
Access `https://localhost:8443/metrics` while the WebUI is running to see Prometheus-formatted metrics.

**2. Run Performance Benchmark:**
```powershell
# Simulate 5 concurrent users making 10 requests each
uv run python scripts/benchmark_inference.py --users 5 --reqs 10
```

#### Step 6: Auto-Evolution
You can run the full cycle (Wash -> Refine -> Index -> Train) either once or periodically.

**1. One-shot Full Cycle:**
```powershell
# Run the entire pipeline once and exit
uv run schedule-sync full-cycle --mode spark --synthesis
```

**2. Periodic Schedule (Agent S):**
```powershell
# Auto-evolve every 24 hours (Scheduler will stay active)
uv run schedule-sync schedule --mode spark --interval 24 --synthesis
```

---

## 🏗️ Project Structure

```
.
├── src/                        # Main AI Stack (Windows)
│   ├── agents/                 # Specialized Agents (A, B, C, D, S)
│   ├── rag/                    # Vector Database logic
│   ├── synthesis/              # AI SFT Refinement
│   ├── config.py               # Path & API configuration
│   └── run_agents.py           # Unified entry point
├── data_processor/             # Data Processing Worker (K8s/WSL)
│   ├── main.py                 # Spark ETL Entry point
│   └── pyproject.toml          # Lightweight Spark dependencies
├── data/                       # Shared Data Storage
│   ├── raw/                    # Input: Git, Jira, Docs
│   ├── cleaned_corpus.jsonl    # Stage 1: Rough cleaned (Spark)
│   ├── sft_train.jsonl         # Stage 2: Refined (LLM)
│   └── faiss_index.bin         # Knowledge Index
├── docs/                       # Technical Documentation
│   └── ARCHITECTURE.md         # Detailed system design
├── .env                        # API Keys (DEEPSEEK_API_KEY)
└── pyproject.toml              # Main project config
```

## 🔧 Troubleshooting

-   **WSL Connection**: Ensure WSL can access `/mnt/c/`.
-   **API Keys**: Ensure `DEEPSEEK_API_KEY` is set in `.env`.
-   **S3/Redis Connection**: If you see connection errors, ensure the Operator is running and the `DataAlchemyStack` is deployed (`kubectl get das`). On Docker Desktop, the services should automatically map to `localhost` via LoadBalancer.
-   **ROCm Hangs**: The system uses `os._exit(0)` to prevent ROCm-related hangs on Windows termination.
