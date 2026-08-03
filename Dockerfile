# Multi-stage Dockerfile for DataAlchemy
# Supports both WebUI and Worker (Coordinator/Training) modes

# ============================================================================
# Stage 1: Base Image with System Dependencies
# ============================================================================
FROM rocm/dev-ubuntu-24.04:7.1.1 AS base

# Install system dependencies.  The AMD development image also carries an
# optional amdgpu repository; it is not needed at runtime and can stall builds
# when that repository is unavailable behind a proxy.
RUN rm -f /etc/apt/sources.list.d/amdgpu.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    # Python/build tools
    python3 \
    python3-pip \
    python3-venv \
    gcc \
    g++ \
    make \
    # Java for Spark compatibility (if needed)
    default-jre-headless \
    # PyTorch ROCm wheels require the runtime math/DNN libraries below.
    miopen-hip \
    hipblas \
    hipfft \
    hiprand \
    hipsparse \
    hipsparselt \
    hipsolver \
    rccl \
    rocfft \
    rocsolver \
    rocsparse \
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

# The AMD base image supplies ROCm runtime libraries. Keeping that layer
# upstream avoids re-downloading multi-gigabyte ROCm packages during builds.
RUN ln -sf /usr/bin/python3 /usr/local/bin/python

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
RUN pip install --no-cache-dir --retries 8 --timeout 120 --break-system-packages uv

# Copy dependency files
COPY pyproject.toml uv.lock README.md ./
COPY src /app/src

# Install dependencies into a virtual environment
# This creates .venv in /app
RUN uv sync --frozen --no-dev
RUN /app/.venv/bin/python -c "import torch; assert torch.version.hip, 'H5 image requires ROCm-enabled PyTorch'"

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

# ============================================================================
# Stage 6: H5 Harness Job Image
# ============================================================================
# This target intentionally reuses the full ROCm/model runtime.  Spark-only
# Dockerfile.harness remains the rough-clean image and must not run LoRA jobs.
FROM runtime AS harness-job

CMD ["python", "-m", "harness.job_runner"]
