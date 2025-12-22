# LoRA Fine-tuning on AMD ROCm (Windows)

This project demonstrates how to fine-tune a Large Language Model (TinyLlama-1.1B) using LoRA (Low-Rank Adaptation) on AMD GPUs using the ROCm platform on Windows. It is specifically optimized for the **AMD Radeon™ 8060S / AI Max+ 395** series.

## Features

- **ROCm Integration**: Uses ROCm-compatible PyTorch for hardware acceleration on Windows.
- **LoRA Optimization**: Implements Phase 2 optimizations, including targeting all linear layers and using a Cosine learning rate scheduler.
- **Dual-Track Data Alchemy**: 
  - **Python Engine**: Zero-dependency ETL for Windows/macOS development
  - **Spark Engine**: Distributed processing for Linux/cloud production
- **SFT Generation**: Synthetic data generation using DeepSeek LLM to transform raw knowledge into QA pairs.
- **Environment Management**: Fully managed by `uv` for reproducible builds.

## Prerequisites

- **AMD GPU**: Compatible with ROCm (e.g., Radeon 7000/8000 series).
- **uv**: [Install uv](https://github.com/astral-sh/uv) for fast Python package management.
- **Java 17** (Optional): Only required if using Spark mode. Install via `winget install Microsoft.OpenJDK.17`.

## Getting Started

### 1. Environment Setup

The project uses a specific ROCm PyTorch build. Initialize the environment with:

```powershell
uv sync
```

### 2. Verify GPU

Ensure your GPU is detected:

```powershell
uv run python test_gpu.py
```

### 3. Data Alchemy (Data Cleaning)

Place your raw data in `data/raw/` subdirectories:
- `data/raw/git_pr/` - Git PR exports (*.json)
- `data/raw/jira/` - Jira issue exports (*.json)
- `data/raw/confluence/` - Confluence page exports (*.json)
- `data/raw/documents/` - PDF and DOCX files

Run the ETL pipeline:

```powershell
# Auto-detect engine (Python on Windows, Spark on Linux)
uv run python -m spark_etl.main

# Force Python engine (recommended for Windows)
uv run python -m spark_etl.main --mode python

# Force Spark engine (requires Java)
uv run python -m spark_etl.main --mode spark
```

#### Dual-Track Architecture

| Mode | Best For | Dependencies | Data Scale |
|------|----------|--------------|------------|
| `--mode python` | Windows, macOS, demos | None | < 1GB |
| `--mode spark` | Linux, cloud, production | Java 17 | 1GB - 100GB+ |
| `--mode auto` | Auto-detect (default) | Varies | Varies |

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed architecture documentation.

### 4. Generate SFT Data (Optional)

Transform cleaned data into instruction-response pairs using an LLM:

```powershell
# 1. Edit spark_etl/config.py to add your DeepSeek API Key
# 2. Run with --sft flag
uv run python -m spark_etl.main --sft

# Test with limited samples first
uv run python -m spark_etl.main --sft --max_samples 5
```

**Output files:**
- `data/train.jsonl` - Cleaned corpus (for Continuing Pre-training)
- `data/sft_train.jsonl` - QA pairs (for Supervised Fine-tuning)

### 5. Start Fine-tuning

Run the training script:

```powershell
uv run python train.py
```

The LoRA adapter will be saved to `./lora-tiny-llama-adapter`.

### 6. Run Inference

Test the fine-tuned model:

```powershell
uv run python inference.py
```

## Project Structure

```
.
├── spark_etl/              # Data Alchemy ETL pipeline
│   ├── main.py             # Entry point with --mode switch
│   ├── config.py           # Configuration (paths, API keys)
│   ├── engines/            # Processing engines
│   │   ├── python_engine.py   # Pure Python (no deps)
│   │   └── spark_engine.py    # PySpark (distributed)
│   ├── cleaners/           # Data source processors
│   └── sft_generator.py    # LLM-based QA generation
├── train.py                # LoRA training script
├── inference.py            # Model inference script
├── data/
│   ├── raw/                # Input: raw heterogeneous data
│   ├── train.jsonl         # Output: cleaned corpus (CPT)
│   └── sft_train.jsonl     # Output: QA pairs (SFT)
├── pyproject.toml          # Dependencies with ROCm wheels
├── ARCHITECTURE.md         # Technical architecture docs
└── README.md               # This file
```

## Troubleshooting

### Windows: Spark/Hadoop Errors

If you see `UnsatisfiedLinkError` or `winutils.exe` errors:

```powershell
# Use Python mode instead (recommended for Windows)
uv run python -m spark_etl.main --mode python
```

### Java Not Found (Spark Mode Only)

```powershell
# Install Java
winget install Microsoft.OpenJDK.17

# Set JAVA_HOME (add to system environment variables)
$env:JAVA_HOME = "C:\Program Files\Microsoft\jdk-17.0.x.x"
```

### GPU Not Detected

Ensure ROCm drivers are installed and `HIP_VISIBLE_DEVICES` is set correctly:

```powershell
$env:HIP_VISIBLE_DEVICES = "0"
uv run python test_gpu.py
```
