# LoRA Fine-tuning on AMD ROCm (Windows)

This project demonstrates how to fine-tune a Large Language Model (TinyLlama-1.1B) using LoRA (Low-Rank Adaptation) on AMD GPUs using the ROCm platform on Windows. It is specifically optimized for the **AMD Radeon™ 8060S / AI Max+ 395** series.

## Features
- **ROCm Integration**: Uses ROCm-compatible PyTorch for hardware acceleration on Windows.
- **LoRA Optimization**: Implements Phase 2 optimizations, including targeting all linear layers and using a Cosine learning rate scheduler.
- **Environment Management**: Fully managed by `uv` for reproducible builds.
- **Compatibility Fixes**: Includes monkeypatches for `torch.distributed` issues common in ROCm Windows builds.

## Prerequisites
- **AMD GPU**: Compatible with ROCm (e.g., Radeon 7000/8000 series).
- **uv**: [Install uv](https://github.com/astral-sh/uv) for fast Python package management.

## Getting Started

### 1. Environment Setup
The project uses a specific ROCm PyTorch build. Initialize the environment with:
```powershell
uv sync
```

### 2. Verify GPU
Ensure your GPU is detected and ROCm is working:
```powershell
uv run python test_gpu.py
```

### 3. Prepare Data
Generate the training dataset (includes custom LoRA-specific samples for better alignment):
```powershell
uv run python prepare_data.py
```

### 4. Start Fine-tuning
Run the training script (300 steps, Rank 16, targeting all linear modules):
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
- `train.py`: Main training script with LoRA configuration and ROCm fixes.
- `prepare_data.py`: Data augmentation and preprocessing.
- `inference.py`: Script for testing the fine-tuned model.
- `pyproject.toml`: Dependency definitions with ROCm wheel URLs.
- `implementation_plan.md`: Detailed technical roadmap.
