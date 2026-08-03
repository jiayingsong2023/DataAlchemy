#!/usr/bin/env bash
# Download the remaining ROCm 7.1.1 runtime packages with resume support.
# This script only downloads files; it does not install packages or modify Docker.

set -euo pipefail

DOWNLOAD_DIR="${ROCM_DOWNLOAD_DIR:-$HOME/Downloads/dataalchemy-h5-debs}"
BASE_URL="https://repo.radeon.com/rocm/apt/7.1.1"

mkdir -p "$DOWNLOAD_DIR"
cd "$DOWNLOAD_DIR"

# The existing local RCCL file has the same version string but a different
# repository artifact/hash. Keep it for recovery and download the current one.
RCCL="rccl_2.27.7.70101-38~24.04_amd64.deb"
if [[ -f "$RCCL" ]] && [[ "$(stat -c '%s' "$RCCL")" == "219504018" ]] \
    && sha256sum --check --status <(printf '%s  %s\n' \
        '8e8b88dff488203d9c1867ad7236a38c437b467083bbcf97b2806cb2ccca5cdd' "$RCCL"); then
    mv "$RCCL" "$RCCL.old.$(date +%Y%m%d%H%M%S)"
fi

download() {
    local file="$1"
    local path="$2"
    echo "Downloading $file (resume enabled)"
    curl --fail --location --retry 50 --retry-all-errors --retry-delay 3 \
        --connect-timeout 30 --continue-at - \
        --output "$file" "$BASE_URL/$path"
}

download "$RCCL" \
    "pool/main/r/rccl/$RCCL"
download "rocfft_1.0.35.70101-38~24.04_amd64.deb" \
    "pool/main/r/rocfft/rocfft_1.0.35.70101-38~24.04_amd64.deb"
download "rocsolver_3.31.0.70101-38~24.04_amd64.deb" \
    "pool/main/r/rocsolver/rocsolver_3.31.0.70101-38~24.04_amd64.deb"
download "rocsparse_4.1.0.70101-38~24.04_amd64.deb" \
    "pool/main/r/rocsparse/rocsparse_4.1.0.70101-38~24.04_amd64.deb"

printf '%s  %s\n' \
    'f6b64b834582a23d52a2f408e190d80936e86a56f703e94d4aac10a9f73e1f98' "$RCCL" \
    'b50f194fc272a10c58658811f4e787c51267047dd738cdaa0d7357e07dcc25a1' 'rocfft_1.0.35.70101-38~24.04_amd64.deb' \
    'dc0e15e02eb2405ff2b845762287a20bea273107b498cd0d7452b5349d9d839' 'rocsolver_3.31.0.70101-38~24.04_amd64.deb' \
    '7e80f7767c5e0287f4e47b03cfccbbc45e57d7da3ca1459617b6542dd47a5e01' 'rocsparse_4.1.0.70101-38~24.04_amd64.deb' \
    | sha256sum --check --strict --quiet

echo "ROCm package download and checksum verification passed: $DOWNLOAD_DIR"
