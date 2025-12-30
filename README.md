# Data Alchemy: Enterprise RAG + LoRA Multi-Agent System

This project is an enterprise-grade AI system that combines **Data Cleaning**, **Multi-Agent Coordination**, **LoRA Fine-tuning**, and **RAG (Retrieval-Augmented Generation)**. Optimized for AMD GPUs on Windows (ROCm), it transforms enterprise internal data (Jira, Git, Docs) into a reliable knowledge assistant.

## 📚 Architecture
![DataAlchemy](https://github.com/user-attachments/assets/e20fdd5f-9329-4988-8c67-fa77a69f1caa)


## 🚀 Key Features

-   **Multi-Agent Architecture**:
    -   **Agent A (Cleaner)**: Hybrid cleaning (WSL/Spark + Windows/LLM).
    -   **Agent B (Trainer)**: Specialized LoRA domain training.
    -   **Agent C (Knowledge)**: FAISS-powered high-speed vector search.
    -   **Agent D (Finalist)**: Intelligent fusion of RAG facts and LoRA intuition.
    -   **Agent S (Scheduler)**: Automates periodic ingestion and training.
-   **Cross-Environment ETL**: Uses Spark in WSL for rough cleaning and LLMs in Windows for refinement, solving dependency conflicts.
-   **ROCm Optimized**: Tailored for AMD Radeon™ GPUs using specific ROCm for Windows wheels.

---

## 🛠️ Getting Started

### 1. Prerequisites
-   **AMD GPU**: Compatible with ROCm.
-   **WSL2**: Installed on Windows.
-   **uv**: [Install uv](https://github.com/astral-sh/uv).

### 2. Environment Setup

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

### 4. Running the Pipeline

The system supports two cleaning modes:
-   **`spark` mode (Recommended)**: Uses Spark in Kubernetes for heavy data cleaning and chunking. Ideal for large datasets.
-   **`python` mode**: Pure Python cleaning on Windows. Zero setup required, ideal for small datasets or quick testing.

#### Step 1: Ingestion (Agent A + Agent C)
Rough cleaning (Spark/Python) -> Refinement (LLM) -> Indexing (FAISS).

**1. Rough Cleaning only (Washing):**
```powershell
# Using Spark (WSL) - Recommended for scale
uv run data-alchemy ingest --mode spark --stage wash

# OR Using Pure Python (Windows) - No WSL required
uv run data-alchemy ingest --mode python --stage wash
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

**2. WebUI Chat (New):**
```powershell
# Start the WebUI server (HTTPS)
uv run python webui/app.py
```
Then open `https://localhost:8443` in your browser. (Note: You may need to accept the self-signed certificate warning).

#### Step 5: Auto-Evolution
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
