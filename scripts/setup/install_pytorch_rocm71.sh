#!/bin/bash
# 手动安装 PyTorch ROCm 7.1 wheels
# 因为 uv 可能无法直接从 AMD 仓库安装

set -e

echo "=========================================="
echo "安装 PyTorch ROCm 7.1 (AMD AI Max+395)"
echo "=========================================="
echo ""

PYTHON_VERSION="cp312"
ARCH="linux_x86_64"
BASE_URL="https://repo.radeon.com/rocm/manylinux/rocm-rel-7.1"

# 尝试不同的版本号
VERSIONS=(
    "2.9.0+rocm7.1.0.gite3c6ee2b"
    "2.8.0+rocm7.1.0.git78f6ff78"
    "2.7.0+rocm7.1.0"
    "2.6.0+rocm7.1.0.lw.git78f6ff78"
)

TORCH_VERSION=""
TORCHVISION_VERSION=""
TORCHAUDIO_VERSION=""

echo "正在查找可用的 PyTorch 版本..."
echo ""

# 尝试找到可用的版本
for version in "${VERSIONS[@]}"; do
    torch_file="torch-${version}-${PYTHON_VERSION}-${PYTHON_VERSION}-${ARCH}.whl"
    torch_url="${BASE_URL}/${torch_file}"
    
    echo "检查: ${torch_file}"
    
    # 使用 wget 或 curl 检查文件是否存在
    if command -v wget &> /dev/null; then
        if wget --spider -q "${torch_url}" 2>/dev/null; then
            TORCH_VERSION="${version}"
            echo "✅ 找到 torch: ${version}"
            break
        fi
    elif command -v curl &> /dev/null; then
        if curl -s -o /dev/null -w "%{http_code}" "${torch_url}" | grep -q "200"; then
            TORCH_VERSION="${version}"
            echo "✅ 找到 torch: ${version}"
            break
        fi
    else
        echo "⚠️  需要 wget 或 curl 来检查文件"
        # 假设使用第一个版本
        TORCH_VERSION="${VERSIONS[0]}"
        break
    fi
done

if [ -z "$TORCH_VERSION" ]; then
    echo "❌ 未找到可用的 PyTorch 版本"
    echo ""
    echo "请手动访问: ${BASE_URL}"
    echo "查找适合 Python 3.12 的 torch wheel 文件"
    exit 1
fi

# 确定对应的 torchvision 和 torchaudio 版本
if [[ "$TORCH_VERSION" == *"2.9.0"* ]]; then
    TORCHVISION_VERSION="0.24.0+rocm7.1.0.gitb919bd0c"
    TORCHAUDIO_VERSION="2.9.0+rocm7.1.0.gite3c6ee2b"
elif [[ "$TORCH_VERSION" == *"2.8.0"* ]]; then
    TORCHVISION_VERSION="0.23.0+rocm7.1.0.git824e8c87"
    TORCHAUDIO_VERSION="2.8.0+rocm7.1.0.git78f6ff78"
elif [[ "$TORCH_VERSION" == *"2.7.0"* ]]; then
    TORCHVISION_VERSION="0.22.1+rocm7.1.0.git59a3e1f9"
    TORCHAUDIO_VERSION="2.7.0+rocm7.1.0"
elif [[ "$TORCH_VERSION" == *"2.6.0"* ]]; then
    TORCHVISION_VERSION="0.21.0+rocm7.1.0.git4040d51f"
    TORCHAUDIO_VERSION="2.6.0+rocm7.1.0.lw.git78f6ff78"
fi

echo ""
echo "使用以下版本:"
echo "  torch: ${TORCH_VERSION}"
echo "  torchvision: ${TORCHVISION_VERSION}"
echo "  torchaudio: ${TORCHAUDIO_VERSION}"
echo ""

# 使用 uv pip 安装
if command -v uv &> /dev/null; then
    echo "使用 uv pip 安装..."
    uv pip install \
        "${BASE_URL}/torch-${TORCH_VERSION}-${PYTHON_VERSION}-${PYTHON_VERSION}-${ARCH}.whl" \
        "${BASE_URL}/torchvision-${TORCHVISION_VERSION}-${PYTHON_VERSION}-${PYTHON_VERSION}-${ARCH}.whl" \
        "${BASE_URL}/torchaudio-${TORCHAUDIO_VERSION}-${PYTHON_VERSION}-${PYTHON_VERSION}-${ARCH}.whl"
else
    echo "使用 pip 安装..."
    pip install \
        "${BASE_URL}/torch-${TORCH_VERSION}-${PYTHON_VERSION}-${PYTHON_VERSION}-${ARCH}.whl" \
        "${BASE_URL}/torchvision-${TORCHVISION_VERSION}-${PYTHON_VERSION}-${PYTHON_VERSION}-${ARCH}.whl" \
        "${BASE_URL}/torchaudio-${TORCHAUDIO_VERSION}-${PYTHON_VERSION}-${PYTHON_VERSION}-${ARCH}.whl"
fi

echo ""
echo "✅ PyTorch ROCm 7.1 安装完成！"
echo ""
echo "验证安装:"
echo "  uv run python -c 'import torch; print(f\"PyTorch: {torch.__version__}\"); print(f\"ROCm available: {torch.cuda.is_available()}\")'"
