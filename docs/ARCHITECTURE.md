# LoRA + RAG Multi-Agent Architecture: Enterprise Knowledge Hub

This document describes the evolved technical architecture of the pipeline, which integrates Data Alchemy, Multi-Agent Coordination, RAG (Retrieval-Augmented Generation), and LoRA Fine-tuning.

## 1. Overall Pipeline (Agentic Workflow)

The system is organized into specialized Agents and a cross-environment data pipeline.

![DataAlchemy](https://github.com/user-attachments/assets/e20fdd5f-9329-4988-8c67-fa77a69f1caa)

## 2. Multi-Agent Roles

### 2.1 Agent A: The Cleaner (Data Alchemy)
- **Responsibility**: Heterogeneous data extraction and cleaning.
- **Cross-Environment Orchestration**: Agent A in the main project acts as a bridge. It triggers the **Spark Standalone Project** in WSL for large-scale rough cleaning.
- **Output**: Produces `cleaned_corpus.jsonl` (Roughly cleaned, desensitized) and `rag_chunks.jsonl` (Semantic chunks for RAG).

### 2.2 Agent B: The Trainer (Domain Specialist)
- **Responsibility**: Managing the LoRA life cycle.
- **Role in Inference**: Provides "Model Intuition". It understands domain-specific terminology and the "style" of the internal data.

### 2.3 Agent C: The Librarian (RAG Manager)
- **Responsibility**: Vector storage and high-speed retrieval.
- **Technology**: **FAISS** + **Sentence-Transformers**.

### 2.4 Agent D: The Finalist (Fusion Expert)
- **Responsibility**: Evidence synthesis and final answering.
- **Strategy**: Hybrid Parallel Fusion. It combines facts from Agent C and reasoning suggestions from Agent B via DeepSeek.

### 2.5 Agent S: The Scheduler (Chronos)
- **Responsibility**: Automated periodic execution (Wash -> Refine -> Train).

---

## 3. Data Flow Specification
# LoRA + RAG Multi-Agent Architecture: Enterprise Knowledge Hub

This document describes the evolved technical architecture of the pipeline, which integrates Data Alchemy, Multi-Agent Coordination, RAG (Retrieval-Augmented Generation), and LoRA Fine-tuning.

## 1. Overall Pipeline (Agentic Workflow)

The system is organized into specialized Agents and a cross-environment data pipeline.

```mermaid
flowchart TD
    AgentS[Agent S: The Scheduler] -->|Periodic Trigger| Coordinator
    
    subgraph WSL_Environment [WSL: Rough Cleaning]
        direction TB
        Raw[Raw Data: Jira, Git, Docs] --> DataProcessor[Data Processor Project]
        DataProcessor --> Washed[data/cleaned_corpus.jsonl]
    end

    subgraph Windows_Environment [Windows: AI Refinement]
        direction TB
        Washed --> Synthesis[LLM Synthesis / SFT Generator]
        Synthesis --> SFT_Data[data/sft_train.jsonl]
        Washed --> RAG_Chunks[data/rag_chunks.jsonl]
        
        subgraph Training [Agent B: The Trainer]
            SFT_Data --> Trainer[train.py]
            Trainer --> Adapter[LoRA Adapter]
        end

        subgraph Knowledge [Agent C: The Librarian]
            RAG_Chunks --> Embedding[Embedding Model]
            Embedding --> FAISS[(FAISS Vector DB)]
        end

        subgraph Inference [Optimized Inference Pipeline]
            Query[User Question] -->|Cache Check| CacheMgr[CacheManager: Redis + Semantic]
            CacheMgr -->|Miss| BatchEngine[BatchInferenceEngine]
            BatchEngine -->|Batch| ModelMgr[ModelManager: torch.compile]
            
            Query -->|Recall| Agent_C
            Agent_C -->|Context| Context[RAG Facts]
            ModelMgr -->|Predict| Agent_B
            Agent_B -->|Intuition| Intuition[LoRA Logic]
            
            Context & Intuition & Query -->|Fusion| Agent_D[Agent D: Finalist]
            Agent_D -->|Final Answer| FinalResponse[Expert Response]
            FinalResponse -->|Store| CacheMgr
        end
    end

    Coordinator[Coordinator: Orchestrator] --> Ingestion_Trigger
    Ingestion_Trigger[Ingest: Agent A] --> WSL_Environment
    WSL_Environment -.->|Shared Filesystem| Windows_Environment
    Coordinator --> Knowledge
    Coordinator --> Inference
```

---

## 2. Multi-Agent Roles

### 2.1 Agent A: The Cleaner (Data Alchemy)
- **Responsibility**: Heterogeneous data extraction and cleaning.
- **Cross-Environment Orchestration**: Agent A in the main project acts as a bridge. It triggers the **Data Processor Project** in WSL for large-scale rough cleaning.
- **Output**: Produces `cleaned_corpus.jsonl` (Roughly cleaned, desensitized) and `rag_chunks.jsonl` (Semantic chunks for RAG).

### 2.2 Agent B: The Trainer (Domain Specialist)
- **Responsibility**: Managing the LoRA life cycle.
- **Role in Inference**: Provides "Model Intuition". It understands domain-specific terminology and the "style" of the internal data.

### 2.3 Agent C: The Librarian (RAG Manager)
- **Responsibility**: Vector storage and high-speed retrieval.
- **Technology**: **FAISS** + **Sentence-Transformers**.

### 2.4 Agent D: The Finalist (Fusion Expert)
- **Responsibility**: Evidence synthesis and final answering.
- **Strategy**: Hybrid Parallel Fusion. It combines facts from Agent C and reasoning suggestions from Agent B via DeepSeek.

### 2.5 Agent S: The Scheduler (Chronos)
- **Responsibility**: Automated periodic execution (Wash -> Refine -> Train).

---

## 3. Data Flow Specification

| Stage | Platform | Engine | Input | Output | Purpose |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Rough Cleaning** | WSL | Spark | `data/raw/*` | `cleaned_corpus.jsonl` | Massive data cleaning & desensitization |
| **Refinement** | Windows | LLM (ETL) | `cleaned_corpus.jsonl` | `sft_train.jsonl` | Generating high-quality QA training pairs |
| **Indexing** | Windows | Agent C | `rag_chunks.jsonl` | FAISS Index | Build the searchable knowledge base |
| **Training** | Windows | Agent B | `sft_train.jsonl` | LoRA Adapter | Fine-tune model on domain patterns |
| **Chat** | Windows | Coordinator | User Query | Final Answer | Combine RAG facts and LoRA intuition |

---

## 4. Dual-Stack Cleaning Engine (Phase 1)

To ensure both high performance for large datasets and zero-dependency ease of use for small datasets, the system implements a "Dual-Stack" cleaning architecture.

### 4.1 Architecture Overview

```mermaid
flowchart LR
    Input[Raw Data] --> AgentA[Agent A: Orchestrator]
    
    AgentA -->|mode='spark'| SparkPath[Spark Stack: WSL/Linux]
    AgentA -->|mode='python'| PythonPath[Python Stack: Windows]
    
    subgraph SparkStack [Spark Stack]
        SparkEngine[SparkEngine]
        SparkEngine --> S_Git[process_git_pr]
        SparkEngine --> S_Jira[process_jira]
        SparkEngine --> S_Doc[process_documents]
        SparkEngine --> S_Feed[process_feedback]
        
        subgraph S_Cleaners [UDFs]
            S_HTML[clean_html_udf]
            S_WS[normalize_whitespace_udf]
        end
    end
    
    subgraph PythonStack [Python Stack]
        PyEngine[PythonEngine]
        PyEngine --> P_JSON[_process_json_files]
        PyEngine --> P_Doc[_process_documents]
        PyEngine --> P_Feed[_process_feedback]
        
        subgraph P_Cleaners [Core Logic]
            P_HTML[clean_html]
            P_WS[normalize_whitespace]
            P_San[sanitize_text]
        end
    end
    
    SparkStack --> Output[cleaned_corpus.jsonl]
    PythonStack --> Output
```

### 4.2 Engine Comparison

| Feature | Spark Engine (`main.py`) | Python Engine (`python_engine.py`) |
| :--- | :--- | :--- |
| **Environment** | WSL / Linux (Big Data Stack) | Windows / macOS (Pure Python) |
| **Data Scale** | > 10GB (Distributed) | < 1GB (Single Machine) |
| **Core Technology** | PySpark DataFrames & UDFs | Standard Python Lists & Dicts |
| **Cleaning Pipeline** | `clean_html` -> `normalize_whitespace` | `clean_html` -> `normalize_whitespace` -> `sanitize_text` |
| **Chunking Strategy** | Spark-native windowing (planned) | Sliding window (`_chunk_text`) |

### 4.3 Key Functions & Logic

#### Shared Cleaners (`data_processor/cleaners/base.py`)
- `clean_html(text)`: Uses `BeautifulSoup` with `html.parser` to strip tags and extract clean text.
- `normalize_whitespace(text)`: Uses `re.sub(r'\s+', ' ', text)` to collapse multiple spaces and newlines.

#### Sanitization (`data_processor/sanitizers.py`)
- `sanitize_text(text)`: Iterates through `PATTERNS` defined in `config.py` (e.g., IP addresses, internal URLs) and replaces them with `TOKENS` (e.g., `[IP_ADDR]`).

#### Spark Engine Specifics
- **UDF Registration**: Cleaners are wrapped in `pyspark.sql.functions.udf` for parallel execution across the Spark cluster.
- **DataFrame Union**: Different sources are processed into identical schemas and combined using `df.union()`.

#### Python Engine Specifics
- **Lazy Loading**: Parsers like `pypdf` and `python-docx` are loaded only when needed to minimize startup time.
- **Memory Efficient**: Uses generators and line-by-line JSON processing to handle files larger than RAM.

---

## 5. Cross-Environment Architecture

To solve dependency conflicts between ROCm (AI) and Spark (Java/Big Data), the project is split:

1.  **Main Project (Windows/ROCm)**: Contains AI Agents (B, C, D), Coordinator, and LLM Refinement logic.
2.  **Data Processor (WSL/Linux)**: A lightweight project in `data_processor/` that only depends on PySpark.
3.  **Infrastructure (Kubernetes)**: MinIO for S3 storage and Redis for inference caching.

**Communication**: Orchestrated via `subprocess` calls using `wsl` command and data exchange via S3 (MinIO) or Redis.

---

## 6. Inference Optimization & Caching

To meet enterprise requirements for high concurrency and low latency, the system implements a multi-tier optimization strategy.

### 6.1 ModelManager (ROCm Acceleration)
- **`torch.compile`**: Uses the Inductor backend to generate optimized Triton/C++ kernels for AMD GPUs.
- **Mixed Precision**: Automatically uses `torch.float16` for inference to reduce VRAM usage and increase throughput.
- **Lazy Loading**: Models are loaded into GPU memory only upon the first request.

### 6.2 BatchInferenceEngine (Throughput)
- **Dynamic Batching**: Accumulates incoming requests into batches (default max 8) within a short window (default 50ms).
- **Async Processing**: Uses `asyncio` to handle multiple concurrent users without blocking the main event loop.

### 6.3 CacheManager (Intelligence)
- **Exact Match**: MD5 hashing of prompt + parameters for instant retrieval.
- **Semantic Search**: Uses `all-MiniLM-L6-v2` to compute query embeddings. If a new query is >92% similar to a cached one, the cached result is returned.
- **Redis Persistence**: All cache entries and the semantic index are persisted to Redis, ensuring survival across restarts.
