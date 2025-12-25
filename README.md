# Data Alchemy: Enterprise RAG + LoRA Multi-Agent System

This project is an enterprise-grade AI system that combines **Data Cleaning**, **Multi-Agent Coordination**, **LoRA Fine-tuning**, and **RAG (Retrieval-Augmented Generation)**. Optimized for AMD GPUs on Windows (ROCm), it transforms enterprise internal data (Jira, Git, Docs) into a reliable knowledge assistant.

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

**Spark Worker (WSL - Data Cleaning):**
```bash
# In WSL
cd /mnt/c/Users/<user>/<project path>/spark_etl_standalone
for example, cd /mnt/c/Users/Administrator/work/lora/spark_etl_standalone
uv sync
```

### 3. Running the Pipeline

The system supports two cleaning modes:
-   **`spark` mode (Recommended)**: Uses Spark in WSL for heavy data cleaning and chunking. Ideal for large datasets.
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

#### Step 3: Interactive Chat
Combine RAG facts and LoRA intuition for expert answers.
```powershell
uv run chat
```

#### Step 4: Auto-Evolution
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
│   ├── etl/                    # Python ETL & SFT Refinement
│   ├── config.py               # Path & API configuration
│   └── run_agents.py           # Unified entry point
├── spark_etl_standalone/       # Spark Worker (WSL)
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
