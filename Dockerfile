# Multi-stage Dockerfile for DataAlchemy
# Builds separate WebUI and governed training/evaluation images.

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
    curl \
    procps \
    ca-certificates \
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
# Stage 2: Builders - Install Only the Target Dependency Group
# ============================================================================
FROM base AS dependency-builder

# Install uv for fast dependency management
RUN pip install --no-cache-dir --retries 8 --timeout 120 --break-system-packages uv

# Copy dependency files
COPY pyproject.toml uv.lock README.md ./

FROM dependency-builder AS web-dependencies
RUN uv sync --frozen --no-default-groups --group web --no-install-project
RUN /app/.venv/bin/python -c "import torch; assert torch.version.hip, 'H5 image requires ROCm-enabled PyTorch'"

# ============================================================================
# Stage 3: Runtime Base
# ============================================================================
FROM base AS runtime

ARG BUILD_GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$BUILD_GIT_SHA
ENV BUILD_GIT_SHA=$BUILD_GIT_SHA

# Copy application code
# Copy project structure
COPY src /app/src
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
FROM web-dependencies AS web-builder

COPY src /app/src
RUN uv sync --frozen --offline --no-default-groups --group web

FROM runtime AS webui

COPY --from=web-builder /app/.venv /app/.venv
COPY webui/ /app/webui/

EXPOSE 8443

# Health check for WebUI
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8443/metrics || exit 1

# Run WebUI server
CMD ["python", "-m", "uvicorn", "webui.app:app", "--host", "0.0.0.0", "--port", "8443"]

# ============================================================================
# Stage 5: H5 Harness Job Image
# ============================================================================
# This target intentionally reuses the full ROCm/model runtime.  Spark-only
# Dockerfile.harness remains the rough-clean image and must not run LoRA jobs.
FROM web-dependencies AS training-dependencies

# Exact sync removes Web-only packages while reusing the shared ROCm wheel cache.
RUN uv sync --frozen --no-default-groups --group training --no-install-project
RUN /app/.venv/bin/python -c "import torch; assert torch.version.hip, 'H5 image requires ROCm-enabled PyTorch'"

FROM training-dependencies AS training-builder

COPY src /app/src
RUN uv sync --frozen --offline --no-default-groups --group training

FROM runtime AS harness-job

COPY --from=training-builder /app/.venv /app/.venv

CMD ["python", "-m", "harness.job_runner"]
