#!/bin/bash
# k3d 集群设置脚本
# 创建 k3d 集群并配置端口映射

set -e

CLUSTER_NAME="${K3D_CLUSTER_NAME:-dataalchemy-gpu}"
GPU_ENABLED="${K3D_GPU_ENABLED:-true}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "=========================================="
echo "k3d 集群设置脚本"
echo "=========================================="
echo ""

# Check if k3d is installed
if ! command -v k3d &> /dev/null; then
    echo "❌ k3d 未安装"
    echo ""
    echo "安装 k3d:"
    echo "  curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash"
    echo ""
    echo "或者使用包管理器:"
    echo "  # Ubuntu/Debian"
    echo "  wget -q -O - https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash"
    exit 1
fi

echo "✅ k3d 已安装: $(k3d version | head -1)"
echo ""

# Check if cluster already exists
if k3d cluster list | grep -q "$CLUSTER_NAME"; then
    echo "⚠️  集群 '$CLUSTER_NAME' 已存在"
    read -p "是否删除并重新创建？(y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "删除现有集群..."
        k3d cluster delete "$CLUSTER_NAME"
    else
        echo "使用现有集群"
        exit 0
    fi
fi

echo "步骤 1: 创建 k3d 集群..."
echo "  集群名称: $CLUSTER_NAME"
echo "  端口映射: 80:80 (Traefik Ingress)"
echo "  数据挂载: $PROJECT_ROOT/data -> /data"
echo ""

GPU_ARGS=()
GPU_VOLUMES=()
if [[ "$GPU_ENABLED" == "true" ]]; then
    if [[ ! -r /etc/cdi/amd.json ]]; then
        echo "❌ AMD CDI spec 不存在: /etc/cdi/amd.json"
        echo "   先运行: sudo amd-ctk cdi generate --output=/etc/cdi/amd.json"
        exit 1
    fi
    if ! docker info --format '{{json .Runtimes}}' | grep -q '"amd"'; then
        echo "❌ Docker 未注册 AMD runtime"
        echo "   先运行: sudo amd-ctk runtime configure && sudo systemctl restart docker"
        exit 1
    fi
    GPU_ARGS+=(--gpus all)
    # k3s runs nested containerd; make the host CDI registry visible to every node.
    GPU_VOLUMES+=(--volume /etc/cdi:/etc/cdi@all)
fi

# Create k3d cluster with port mappings
k3d cluster create "$CLUSTER_NAME" \
    --port "80:80@loadbalancer" \
    --volume "$PROJECT_ROOT/data:/data@all" \
    "${GPU_VOLUMES[@]}" \
    "${GPU_ARGS[@]}" \
    --wait

if [[ "$GPU_ENABLED" == "true" ]]; then
    SERVER_CONTAINER="k3d-${CLUSTER_NAME}-server-0"
    if ! docker exec "$SERVER_CONTAINER" test -r /etc/cdi/amd.json; then
        echo "❌ K3d 节点未挂载 AMD CDI spec"
        exit 1
    fi
    if ! docker exec "$SERVER_CONTAINER" test -c /dev/kfd || \
       ! docker exec "$SERVER_CONTAINER" test -c /dev/dri/renderD128; then
        echo "❌ K3d 节点未获得 AMD GPU 设备"
        exit 1
    fi
    echo "✅ K3d 节点已注入 AMD CDI spec 与 GPU 设备"
fi

echo ""
echo "✅ k3d 集群创建成功！"
echo ""

# Set kubeconfig
export KUBECONFIG=$(k3d kubeconfig write "$CLUSTER_NAME")
echo "KUBECONFIG 已设置为: $KUBECONFIG"

echo ""
echo "步骤 2: 验证集群..."
kubectl cluster-info
kubectl get nodes

echo ""
echo "=========================================="
echo "✅ k3d 集群设置完成！"
echo "=========================================="
echo ""
echo "下一步:"
echo "1. 部署整个 DataAlchemy 栈 (推荐):"
echo "   ./scripts/helm-deploy.sh"
echo ""
echo "2. 检查服务状态:"
echo "   kubectl get pods -n data-alchemy"
echo ""
echo "服务访问 (部署完成后):"
echo "  WebUI: http://data-alchemy.test"
echo "  MinIO Console: http://minio-console.test"
echo ""
