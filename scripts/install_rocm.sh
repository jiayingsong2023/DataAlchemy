#!/bin/bash
# ROCm 安装脚本 for AMD AI Max+395
# 适用于 Ubuntu 24.04.3 LTS

set -e

echo "=========================================="
echo "AMD AI Max+395 ROCm 安装脚本"
echo "=========================================="
echo ""

# 检查是否为 root
if [ "$EUID" -eq 0 ]; then 
   echo "❌ 请不要使用 root 用户运行此脚本"
   echo "   请使用普通用户运行，脚本会在需要时提示输入密码"
   exit 1
fi

# 检查 Ubuntu 版本
if [ ! -f /etc/os-release ]; then
    echo "❌ 无法检测操作系统版本"
    exit 1
fi

. /etc/os-release
echo "✅ 检测到系统: $PRETTY_NAME"

if [[ "$VERSION_ID" != "24.04" ]]; then
    echo "⚠️  警告: 此脚本针对 Ubuntu 24.04 设计"
    echo "   当前版本: $VERSION_ID"
    read -p "是否继续? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo ""
echo "步骤 1/5: 更新系统包列表..."
sudo apt update

echo ""
echo "步骤 2/5: 安装必要的依赖..."
sudo apt install -y \
    wget \
    gnupg2 \
    software-properties-common \
    clinfo

echo ""
echo "步骤 3/5: 添加 AMD ROCm 官方仓库..."

# 检查是否已添加仓库
if [ -f /etc/apt/sources.list.d/rocm.list ]; then
    echo "⚠️  ROCm 仓库已存在，跳过添加"
else
    # 添加 GPG 密钥
    wget -qO - https://repo.radeon.com/rocm/rocm.gpg.key | sudo apt-key add -
    
    # 添加仓库
    echo 'deb [arch=amd64] https://repo.radeon.com/rocm/apt/7.1/ jammy main' | sudo tee /etc/apt/sources.list.d/rocm.list
    
    echo "✅ ROCm 仓库已添加"
fi

echo ""
echo "步骤 4/5: 更新包列表并安装 ROCm..."
sudo apt update

# 安装 ROCm 核心组件
# Note: In ROCm 7.1, rocm-dkms may not be available, use rocm-core or rocm meta package
echo "正在安装 ROCm 核心组件..."

# 首先尝试安装 rocm meta package (包含所有核心组件)
if apt-cache show rocm &>/dev/null; then
    echo "✅ 找到 rocm meta package，安装中..."
    sudo apt install -y rocm
else
    echo "⚠️  rocm meta package 不可用，尝试安装 rocm-core..."
    if apt-cache show rocm-core &>/dev/null; then
        sudo apt install -y rocm-core
    else
        echo "⚠️  rocm-core 也不可用，尝试安装 rocm-dkms..."
        sudo apt install -y rocm-dkms || echo "⚠️  rocm-dkms 也不可用，继续安装其他组件..."
    fi
fi

# 安装其他 ROCm 组件（如果还没有通过 meta package 安装）
echo "正在安装其他 ROCm 工具..."
sudo apt install -y \
    rocm-dev \
    rocm-libs \
    rocm-utils \
    rocminfo \
    rocm-smi 2>/dev/null || echo "⚠️  某些包可能已通过 meta package 安装或不可用"

echo ""
echo "步骤 5/5: 配置用户权限..."
# 检查用户是否已在组中
if groups | grep -q "\brender\b" && groups | grep -q "\bvideo\b"; then
    echo "✅ 用户已在 render 和 video 组中"
else
    echo "📝 将用户 $USER 添加到 render 和 video 组..."
    sudo usermod -a -G render,video $USER
    echo "✅ 用户组已更新"
    echo ""
    echo "⚠️  重要: 需要重新登录或重启系统才能使权限生效！"
fi

echo ""
echo "=========================================="
echo "✅ ROCm 安装完成！"
echo "=========================================="
echo ""
echo "下一步操作:"
echo "1. 重新登录或重启系统（使权限生效）"
echo "2. 安装 uv（如果还没有）:"
echo "   ./scripts/install_uv.sh"
echo "3. 运行以下命令验证 ROCm 安装:"
echo "   rocm-smi"
echo "   rocminfo"
echo ""
echo "4. 同步项目依赖（使用 uv）:"
echo "   uv sync"
echo ""
echo "5. 运行 GPU 检测脚本:"
echo "   uv run python scripts/test_gpu.py"
echo ""
echo "如果遇到问题，请查看: docs/AMD_AI_MAX_395_ROCm_SETUP.md"
echo ""
