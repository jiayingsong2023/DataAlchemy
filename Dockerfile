# Multi-stage Dockerfile for DataAlchemy
# Supports both WebUI and Worker (Coordinator/Training) modes

# ============================================================================
# Stage 1: Base Image with System Dependencies
# ============================================================================
FROM python:3.12-slim AS base

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Build tools
    gcc \
    g++ \
    make \
    # Java for Spark compatibility (if needed)
    default-jre-headless \
    # Utilities
    wget \
    curl \
    procps \
    git \
    gnupg \
    ca-certificates \
    && curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl \
    && rm -rf /var/lib/apt/lists/*

# Install ROCm runtime libraries (minimal for PyTorch)
# Using ROCm 7.1 for AMD AI Max+ 395 compatibility
# rocm-libs includes rocblas, rocfft, rocrand, rocsparse, miopen-hip, etc.
RUN wget -qO - https://repo.radeon.com/rocm/rocm.gpg.key | gpg --dearmor -o /usr/share/keyrings/rocm-archive-keyring.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/rocm-archive-keyring.gpg] https://repo.radeon.com/rocm/apt/7.1 jammy main" > /etc/apt/sources.list.d/rocm.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    rocm-core \
    rocm-libs \
    roctracer \
    rocprofiler-register \
    && rm -rf /var/lib/apt/lists/*

# Set ROCm environment variables
ENV ROCM_PATH=/opt/rocm
ENV PATH=$ROCM_PATH/bin:$PATH
ENV LD_LIBRARY_PATH=$ROCM_PATH/lib:$LD_LIBRARY_PATH

WORKDIR /app

# ============================================================================
# Stage 2: Builder - Install Python Dependencies
# ============================================================================
FROM base AS builder

# Install uv for fast dependency management
RUN pip install --no-cache-dir uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies into a virtual environment
# This creates .venv in /app
RUN uv sync --frozen --no-dev

# ============================================================================
# Stage 3: Runtime Base
# ============================================================================
FROM base AS runtime

# Copy the virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code
# Copy project structure
COPY src /app/src
COPY webui/ /app/webui/
COPY pyproject.toml uv.lock /app/
# etl is now inside src/etl
COPY models.yaml /app/models.yaml

# Copy .env if exists (optional, K8s provides env vars)
RUN touch /app/.env

# Set Python path to use the virtual environment AND include src for imports
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app:/app/src"

# Create data directories
RUN mkdir -p /app/data/raw /app/data/processed /app/data/models

# ============================================================================
# Stage 4: WebUI Image
# ============================================================================
FROM runtime AS webui

EXPOSE 8443

# Health check for WebUI
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8443/metrics || exit 1

# Run WebUI server
CMD ["python", "-m", "uvicorn", "webui.app:app", "--host", "0.0.0.0", "--port", "8443"]

# ============================================================================
# Stage 5: Worker Image (Coordinator/Training)
# ============================================================================
FROM runtime AS worker

# Default command runs the coordinator in schedule mode
# Can be overridden at runtime for different commands
CMD ["python", "src/run_agents.py", "schedule", "--interval", "24", "--synthesis"]
