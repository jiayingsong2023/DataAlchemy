#!/bin/bash
# 安装 Kubernetes 工具 (k3d, kubectl, Docker)
# 适用于 Ubuntu 24.04

set -e

echo "=========================================="
echo "Kubernetes 工具安装脚本"
echo "=========================================="
echo ""

# Check what's already installed
INSTALLED=()
MISSING=()

if command -v docker &> /dev/null; then
    INSTALLED+=("docker")
    echo "✅ Docker 已安装: $(docker --version)"
else
    MISSING+=("docker")
    echo "❌ Docker 未安装"
fi

if command -v kubectl &> /dev/null; then
    INSTALLED+=("kubectl")
    echo "✅ kubectl 已安装: $(kubectl version --client --short 2>/dev/null || echo 'installed')"
else
    MISSING+=("kubectl")
    echo "❌ kubectl 未安装"
fi

if command -v k3d &> /dev/null; then
    INSTALLED+=("k3d")
    echo "✅ k3d 已安装: $(k3d version | head -1)"
else
    MISSING+=("k3d")
    echo "❌ k3d 未安装"
fi

echo ""

if [ ${#MISSING[@]} -eq 0 ]; then
    echo "✅ 所有工具已安装！"
    exit 0
fi

echo "需要安装: ${MISSING[*]}"
echo ""

read -p "是否继续安装？(y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    exit 0
fi

# Install Docker
if [[ " ${MISSING[@]} " =~ " docker " ]]; then
    echo ""
    echo "步骤 1/3: 安装 Docker..."
    
    # Check if Docker is installed but not in PATH
    if systemctl is-active --quiet docker 2>/dev/null; then
        echo "✅ Docker 服务正在运行"
    else
        echo "安装 Docker..."
        sudo apt update
        sudo apt install -y docker.io
        sudo systemctl enable docker
        sudo systemctl start docker
        
        # Add user to docker group
        sudo usermod -aG docker $USER
        echo "✅ Docker 已安装"
        echo "⚠️  需要重新登录或运行: newgrp docker"
    fi
fi

# Install kubectl
if [[ " ${MISSING[@]} " =~ " kubectl " ]]; then
    echo ""
    echo "步骤 2/3: 安装 kubectl..."
    
    # Download kubectl
    KUBECTL_VERSION=$(curl -L -s https://dl.k8s.io/release/stable.txt)
    curl -LO "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/amd64/kubectl"
    sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
    rm kubectl
    
    echo "✅ kubectl 已安装: $(kubectl version --client --short)"
fi

# Install k3d
if [[ " ${MISSING[@]} " =~ " k3d " ]]; then
    echo ""
    echo "步骤 3/3: 安装 k3d..."
    
    # Use official install script
    curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
    
    echo "✅ k3d 已安装: $(k3d version | head -1)"
fi

echo ""
echo "=========================================="
echo "✅ 安装完成！"
echo "=========================================="
echo ""
echo "如果 Docker 是新安装的，请重新登录或运行:"
echo "  newgrp docker"
echo ""
echo "然后可以创建 k3d 集群:"
echo "  ./scripts/setup/setup_k3d.sh"
echo ""
