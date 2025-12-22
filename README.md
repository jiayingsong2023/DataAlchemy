# LoRA Fine-tuning on AMD ROCm (Windows)

This project demonstrates how to fine-tune a Large Language Model (TinyLlama-1.1B) using LoRA (Low-Rank Adaptation) on AMD GPUs using the ROCm platform on Windows. It is specifically optimized for the **AMD Radeon™ 8060S / AI Max+ 395** series.

## Features
- **ROCm Integration**: Uses ROCm-compatible PyTorch for hardware acceleration on Windows.
- **LoRA Optimization**: Implements Phase 2 optimizations, including targeting all linear layers and using a Cosine learning rate scheduler.
- **Data Alchemy (Spark ETL)**: Distributed-style data cleaning for heterogeneous sources (Git, Jira, Confluence, PDF, DOCX).
- **SFT Generation**: Synthetic data generation using DeepSeek LLM to transform raw knowledge into QA pairs.
- **Environment Management**: Fully managed by `uv` for reproducible builds.

## Prerequisites
- **AMD GPU**: Compatible with ROCm (e.g., Radeon 7000/8000 series).
- **uv**: [Install uv](https://github.com/astral-sh/uv) for fast Python package management.
- **Java 17**: Required for Spark. Install via `winget install Microsoft.OpenJDK.17`.

## Getting Started

### 1. Environment Setup
The project uses a specific ROCm PyTorch build and PySpark. Initialize the environment with:
```powershell
uv sync
```

### 2. Verify GPU & Environment
Ensure your GPU and Spark environment are ready:
```powershell
uv run python test_gpu.py
# Verify Spark (requires JAVA_HOME to be set)
uv run python -c "from pyspark.sql import SparkSession; SparkSession.builder.master('local').getOrCreate().stop()"
```

### 3. Data Alchemy (Data Cleaning)
Place your raw data (JSON, PDF, DOCX) in `data/raw/` and run the Spark ETL:

```powershell
# Basic cleaning (Git, Jira, Confluence, Documents)
uv run python -m spark_etl.main

# (Optional) Generate SFT QA pairs using DeepSeek
# 1. Edit spark_etl/config.py to add your API Key
# 2. Run with --sft flag
uv run python -m spark_etl.main --sft
```
- Cleaned corpus: `data/train.jsonl`
- Generated SFT data: `data/sft_train.jsonl`

### 4. Start Fine-tuning
Run the training script:
```powershell
uv run python train.py
```
The LoRA adapter will be saved to `./lora-tiny-llama-adapter`.

### 5. Run Inference
Test the fine-tuned model:
```powershell
uv run python inference.py
```

## Project Structure
- `spark_etl/`: Spark-based data cleaning and LLM SFT generation pipeline.
- `train.py`: Main training script with LoRA configuration and ROCm fixes.
- `prepare_data.py`: Legacy data preparation script.
- `inference.py`: Script for testing the fine-tuned model.
- `data/`:
  - `raw/`: Input directory for raw heterogeneous data.
  - `train.jsonl`: Output of Spark ETL (Continuing Pre-training corpus).
  - `sft_train.jsonl`: Output of LLM SFT generator.
- `pyproject.toml`: Dependency definitions with ROCm wheel URLs.
