# syntax=docker/dockerfile:1

ARG PYTHON_IMAGE=python:3.12-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc
ARG ROCM_IMAGE=rocm/dev-ubuntu-24.04:7.2@sha256:749f9ee120c739682cc2e1553e62632c2676f98bc49e4d8133f380e0af682bcc
ARG GPU_RUNTIME_IMAGE=ghcr.io/jiayingsong2023/data-alchemy@sha256:06cb6b58fa2c3ebab94fe636dfa4db9f18200e75f1f76eb303965578aef62357

FROM ${PYTHON_IMAGE} AS web-builder
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-default-groups --group web --no-install-project

FROM ${PYTHON_IMAGE} AS webui
ARG BUILD_GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$BUILD_GIT_SHA
LABEL org.opencontainers.image.source="https://github.com/jiayingsong2023/DataAlchemy"
ENV BUILD_GIT_SHA=$BUILD_GIT_SHA PATH=/app/.venv/bin:$PATH PYTHONPATH=/app:/app/src
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY --from=web-builder /app/.venv /app/.venv
COPY src /app/src
COPY webui /app/webui
COPY pyproject.toml uv.lock models.yaml ./
RUN touch /app/.env && mkdir -p /app/data/raw /app/data/processed /app/data/models
EXPOSE 8443
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 CMD curl -f http://localhost:8443/metrics || exit 1
CMD ["python", "-m", "uvicorn", "webui.app:app", "--host", "0.0.0.0", "--port", "8443"]

FROM ${ROCM_IMAGE} AS gpu-runtime-build
# Keep only code objects for the qualified Radeon 8060S target.
ARG ROCM_GPU_ARCH=gfx1151
WORKDIR /app
RUN rm -f /etc/apt/sources.list.d/amdgpu.list \
    && apt-get update && apt-get install -y --no-install-recommends \
       python3 python3-pip python3-venv curl ca-certificates \
       miopen-hip hipblas hipfft hiprand hipsparse hipsparselt hipsolver rccl rocfft rocsolver rocsparse \
    && rm -rf /opt/rocm/share/miopen/db/* /opt/rocm/lib/rocfft/rocfft_kernel_cache.db \
    && find /opt/rocm/lib/hipblaslt/library -maxdepth 1 -type f \
       ! -name "*${ROCM_GPU_ARCH}*" ! -name '*Mapping*' -delete \
    && find /opt/rocm/lib/rocblas/library -maxdepth 1 -type f \
       ! -name "*${ROCM_GPU_ARCH}*" ! -name '*fallback*' -delete \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/local/bin/python \
    && pip install --no-cache-dir --break-system-packages uv
ARG TORCH_WHEEL_URL=https://repo.radeon.com/rocm/manylinux/rocm-rel-7.1/torch-2.9.1+rocm7.1.0.lw.git351ff442-cp312-cp312-linux_x86_64.whl
ARG TORCH_WHEEL_SHA256=bff09fce55656db5954b7b79b007994d8421c9ef718e5681f686951af8b2a7ad
ARG TORCH_WHEEL_SIZE=1541139407
ARG TRITON_WHEEL_URL=https://repo.radeon.com/rocm/manylinux/rocm-rel-7.1/triton-3.5.1+rocm7.1.0.gita272dfa8-cp312-cp312-linux_x86_64.whl
ARG TRITON_WHEEL_SHA256=ca50f1cbe8a92fb9976959c7d8ad4d60ec701d452cd4035b27db3153e19ef5f1
ARG TRITON_WHEEL_SIZE=287185318
ENV ROCM_PATH=/opt/rocm PATH=/app/.venv/bin:/opt/rocm/bin:$PATH LD_LIBRARY_PATH=/opt/rocm/lib PYTHONPATH=/app:/app/src UV_HTTP_TIMEOUT=600
RUN set -eu; \
    download_ranges() { \
      url="$1"; size="$2"; output="$3"; chunk=67108864; start=0; part=0; active=0; \
      while [ "$start" -lt "$size" ]; do \
        end=$((start + chunk - 1)); [ "$end" -lt "$size" ] || end=$((size - 1)); \
        curl --fail --location --silent --show-error --retry 100 --retry-all-errors \
          --range "$start-$end" --output "${output}.part.$(printf '%05d' "$part")" "$url" & \
        start=$((end + 1)); part=$((part + 1)); active=$((active + 1)); \
        if [ "$active" -eq 8 ]; then wait; active=0; fi; \
      done; \
      [ "$active" -eq 0 ] || wait; \
      cat "${output}".part.* > "$output"; rm "${output}".part.*; \
    }; \
    python -m venv /app/.venv; \
    download_ranges "$TORCH_WHEEL_URL" "$TORCH_WHEEL_SIZE" /tmp/torch-2.9.1+rocm7.1.0.lw.git351ff442-cp312-cp312-linux_x86_64.whl; \
    echo "$TORCH_WHEEL_SHA256  /tmp/torch-2.9.1+rocm7.1.0.lw.git351ff442-cp312-cp312-linux_x86_64.whl" | sha256sum --check -; \
    download_ranges "$TRITON_WHEEL_URL" "$TRITON_WHEEL_SIZE" /tmp/triton-3.5.1+rocm7.1.0.gita272dfa8-cp312-cp312-linux_x86_64.whl; \
    echo "$TRITON_WHEEL_SHA256  /tmp/triton-3.5.1+rocm7.1.0.gita272dfa8-cp312-cp312-linux_x86_64.whl" | sha256sum --check -; \
    uv pip install --python /app/.venv/bin/python --no-deps /tmp/*.whl; \
    find /app/.venv/lib/python3.12/site-packages/torch/lib/aotriton.images \
      -mindepth 1 -maxdepth 1 ! -name amd-gfx11xx -exec rm -rf '{}' +; \
    rm -rf /app/.venv/lib/python3.12/site-packages/triton/backends/nvidia \
      /app/.venv/lib/python3.12/site-packages/torch/test \
      /app/.venv/lib/python3.12/site-packages/torch/include; \
    rm /tmp/*.whl
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --inexact --no-default-groups --group inference --no-install-project \
      --no-install-package torch --no-install-package triton \
    && /app/.venv/bin/python -c "import prometheus_client, redis, torch; assert torch.version.hip"

FROM ${GPU_RUNTIME_IMAGE} AS gpu-runtime

FROM gpu-runtime AS inference
ARG BUILD_GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$BUILD_GIT_SHA
LABEL org.opencontainers.image.source="https://github.com/jiayingsong2023/DataAlchemy"
ENV BUILD_GIT_SHA=$BUILD_GIT_SHA
COPY src /app/src
COPY models.yaml /app/models.yaml
RUN touch /app/.env && mkdir -p /app/data/models
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD curl -f http://localhost:8001/health || exit 1
CMD ["python", "-m", "uvicorn", "inference.service:app", "--host", "0.0.0.0", "--port", "8001"]

FROM gpu-runtime AS harness-job
ARG BUILD_GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$BUILD_GIT_SHA
LABEL org.opencontainers.image.source="https://github.com/jiayingsong2023/DataAlchemy"
ENV BUILD_GIT_SHA=$BUILD_GIT_SHA
RUN uv sync --frozen --inexact --no-default-groups --group inference --group training \
    --no-install-project --no-install-package torch --no-install-package triton
COPY src /app/src
COPY models.yaml /app/models.yaml
COPY scripts/compile_sft_experiences.py /app/scripts/compile_sft_experiences.py
RUN touch /app/.env && mkdir -p /app/data/models
CMD ["python", "-m", "harness.job_runner"]
