# Multi-Agent LoRA + RAG Knowledge Hub (AMD ROCm)

This project is an enterprise-grade AI system that combines **Multi-Agent Coordination**, **LoRA Fine-tuning**, and **RAG (Retrieval-Augmented Generation)**. Optimized for AMD GPUs on Windows (ROCm), it transforms internal data (Jira, Git, Docs) into a reliable knowledge assistant.

## 🚀 Key Features

- **Multi-Agent Architecture**: 
    - **Agent A (Cleaner)**: Dual-track cleaning for SFT and RAG.
    - **Agent B (Trainer)**: Specialized LoRA domain training.
    - **Agent C (Knowledge)**: FAISS-powered high-speed vector search.
    - **Agent D (Finalist)**: Intelligent fusion of RAG facts and LoRA intuition.
    - **Agent S (Scheduler)**: Automates periodic data ingestion and training.
- **RAG + LoRA Fusion**: Uses a hybrid approach where RAG provides the "facts" and LoRA provides the "domain understanding".
- **FAISS Vector DB**: Locally managed, persistent vector storage.
- **ROCm Optimized**: Tailored for AMD Radeon™ 8060S / AI Max+ 395.

## 🛠️ Prerequisites

- **AMD GPU**: Compatible with ROCm (e.g., Radeon 7000/8000 series).
- **uv**: [Install uv](https://github.com/astral-sh/uv).
- **FAISS**: Installed via `uv sync`.
- **API Key**: Required for Synthesis and Agent D.

## ⚙️ Configuration

1. **Create .env file**: Copy `.env.example` to `.env` or create it manually in the project root.
   ```env
   DEEPSEEK_API_KEY=your_actual_key_here
   ```
2. **Security**: The `.env` file is ignored by Git to prevent leaking your keys.

## 🚦 Getting Started

### 1. Environment Setup
```powershell
uv sync
```

### 2. Running the Agentic Pipeline
The system is controlled via a unified entry point. You can use the convenience commands defined in `pyproject.toml`:

#### Step 1: Ingestion (Agent A + Agent C)
Clean raw data, synthesize knowledge via LLM (optional), and build the FAISS vector index.
```powershell
# Basic ingestion (cleaning + indexing)
uv run data-alchemy ingest

# Ingestion with LLM Synthesis (generate SFT data)
uv run data-alchemy ingest --synthesis --max_samples 10
```

#### Step 2: Training (Agent B)
Perform LoRA fine-tuning on the cleaned corpus.
```powershell
uv run train-lora
```

#### Step 3: Interactive Chat (Agent B + C + D)
Start the multi-agent chat interface.
```powershell
uv run chat
```

#### Step 4: Auto-Evolution (Agent S)
Enable the scheduler to automatically run ingest and train periodically.
```powershell
# Auto-evolve every 24 hours
uv run schedule-sync schedule --interval 24

# Auto-evolve with LLM synthesis enabled
uv run schedule-sync schedule --interval 24 --synthesis
```

## 🏗️ Project Structure

```
.
├── src/                    # Source code
│   ├── agents/             # Multi-Agent Implementations
│   │   ├── coordinator.py  # Task Orchestrator
│   │   ├── agent_a.py      # Data cleaning logic
│   │   ├── agent_b.py      # Model intuition & training
│   │   ├── agent_c.py      # Vector search & Rerank
│   │   └── agent_d.py      # Result fusion via DeepSeek
│   ├── rag/                # Vector Database Core
│   │   ├── vector_store.py
│   │   └── retriever.py
│   ├── spark_etl/          # ETL Engines
│   ├── run_agents.py       # Unified Entry Point logic
│   ├── train.py            # LoRA Training script
│   └── inference.py        # Chat interface script
├── docs/                   # Documentation & Research
│   ├── ARCHITECTURE.md
│   ├── Data_Alchemy.txt
│   └── implementation_plan.md
├── scripts/                # Utility & Test scripts
│   ├── test_gpu.py
│   └── check_torch.py
├── data/                   # Data Storage (Local)
│   ├── raw/                # Input data
│   ├── train.jsonl
│   ├── rag_chunks.jsonl
│   └── faiss_index.bin
├── pyproject.toml
└── README.md
```

## 🧠 How Fusion Works
When you ask a question:
1. **Agent C** retrieves the most relevant documentation chunks from FAISS.
2. **Agent B** generates a preliminary answer based on its fine-tuned weights (LoRA Intuition).
3. **Agent D** receives the user query, the RAG evidence, and the LoRA intuition.
4. **DeepSeek** performs the final synthesis, prioritizing facts from RAG while using LoRA's domain understanding.

## 🔧 Troubleshooting
- **Conflict in Dependencies**: The project requires `python == 3.12`. `uv sync` will handle this automatically.
- **Index Not Found**: Ensure you run `ingest` before `chat`.
- **API Errors**: Ensure your `DEEPSEEK_API_KEY` is correctly set in the `.env` file.
