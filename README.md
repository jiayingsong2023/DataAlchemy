# Data Alchemy: Enterprise RAG + LoRA Multi-Agent System

This project is an enterprise-grade AI system that combines **Data Cleaning**, **Multi-Agent Coordination**, **LoRA Fine-tuning**, and **RAG (Retrieval-Augmented Generation)**. Optimized for AMD GPUs on Windows (ROCm), it transforms enterprise internal data (Jira, Git, Docs) into a reliable knowledge assistant.

## 📚 Architecture
![DataAlchemy](https://github.com/user-attachments/assets/e20fdd5f-9329-4988-8c67-fa77a69f1caa)


## 🚀 Key Features

-   **Multi-Agent Architecture**:
    -   **Agent A (Cleaner)**: High-performance cleaning via Spark on Kubernetes.
    -   **Agent B (Trainer)**: Specialized LoRA domain training.
    -   **Agent C (Knowledge)**: FAISS-powered high-speed vector search.
    -   **Agent D (Finalist)**: Intelligent fusion of RAG facts and LoRA intuition.
    -   **Agent S (Scheduler)**: Automates periodic ingestion and training.
-   **Optimized Inference Engine**:
    -   **AMD GPU Acceleration**: Leverages `torch.compile` (Inductor) and FP16 mixed-precision for ROCm.
    -   **Dynamic Batching**: High-throughput inference with `BatchInferenceEngine`.
    -   **Intelligent Caching**: Redis-backed persistence with **Semantic Search** (sentence-transformers).
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

**Kubernetes Cluster Initialization (One-time):**
If you are using **k3d**, initialize the cluster with the provided config to ensure proper volume mapping:
```bash
k3d cluster create --config k8s/cluster-config.yaml
```

**Main Project (Windows - AI & Refinement):**
```powershell
uv sync
```

**Spark Worker / Cluster (Data Cleaning):**

If you are using Docker Desktop K8s, build the image locally to enable the Spark on Kubernetes mode:
```bash
cd data_processor
docker build -t data-processor:latest .
```
> [!IMPORTANT]
> **Developer Note**: If you modify any logic in `data_processor/` (e.g., adding new cleaners or changing sanitization rules), you **must** rebuild the image for the changes to take effect in the cluster.

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

### 4. S3/MinIO Setup (For Production-Grade Data Pipeline)

The system now supports S3-compatible storage (MinIO) for data passing between stages, replacing the shared filesystem approach. This is more suitable for industrial/production environments.

#### One-time Setup:

**1. Deploy MinIO to Kubernetes:**
```bash
kubectl apply -f k8s/minio.yaml
```

**2. Start Port Forwarding (Keep this running in a separate terminal):**
```bash
kubectl port-forward svc/minio 9000:9000 9001:9001
```

#### Data Upload Workflow:

Before running data processing commands, you need to upload your raw data to MinIO:

```bash
# Upload local data/raw to MinIO (s3://lora-data/raw)
uv run python scripts/manage_minio.py upload
```

**Verify uploaded data:**
```bash
uv run python scripts/manage_minio.py list
```

> [!NOTE]
> **When to Re-upload**: If you modify files in `data/raw`, run the upload command again to sync changes to MinIO.

### 5. Running the Pipeline

The system uses **Spark in Kubernetes** for heavy data cleaning and chunking. This distributed mode is ideal for large datasets and is configured to run with multiple executor pods.

#### Step 1: Ingestion (Agent A + Agent C)
Rough cleaning (Spark) -> Refinement (LLM) -> Indexing (FAISS).

**1. Rough Cleaning only (Washing):**
```powershell
uv run data-alchemy ingest --mode spark --stage wash
```
-   Produces `data/cleaned_corpus.jsonl` (for SFT) and `data/rag_chunks.jsonl` (for RAG).

**2. Refinement & Indexing only:**
```powershell
# Convert rough data to SFT pairs and build knowledge index
uv run data-alchemy ingest --stage refine --synthesis --max_samples 50
```
-   Expects `cleaned_corpus.jsonl` and `rag_chunks.jsonl` to exist.

**3. Full Ingestion Pipeline (Default):**
```powershell
# Rough cleaning + LLM Synthesis + FAISS Indexing in one go
uv run data-alchemy ingest --mode spark --synthesis --max_samples 50
```
-   **Rough Cleaning**: `Agent A` produces `data/cleaned_corpus.jsonl`.
-   **Refinement**: `SFT Generator` converts rough data into `data/sft_train.jsonl`.
-   **Indexing**: `Agent C` builds FAISS index from `data/rag_chunks.jsonl`.

#### Step 2: Training (Agent B)
Fine-tune the model using the refined SFT data.
```powershell
uv run train-lora
```

#### Step 4: Interactive Chat
Combine RAG facts and LoRA intuition for expert answers.

**1. Command Line Chat:**
```powershell
uv run chat
```

**2. WebUI Chat (Async & Streaming):**
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
-   **ROCm Hangs**: The system uses `os._exit(0)` to prevent ROCm-related hangs on Windows termination.
