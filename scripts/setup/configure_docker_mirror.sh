#!/bin/bash
# 配置 Docker 镜像加速器（适用于中国网络环境）

set -e

echo "=========================================="
echo "配置 Docker 镜像加速器"
echo "=========================================="
echo ""

# 创建 Docker daemon 配置目录
sudo mkdir -p /etc/docker

# 检查是否已有配置
if [ -f /etc/docker/daemon.json ]; then
    echo "⚠️  发现现有配置 /etc/docker/daemon.json"
    echo "备份为 /etc/docker/daemon.json.bak"
    sudo cp /etc/docker/daemon.json /etc/docker/daemon.json.bak
fi

# 创建新的配置（使用阿里云和腾讯云镜像加速器）
echo "创建 Docker 镜像加速器配置..."
sudo tee /etc/docker/daemon.json > /dev/null <<EOF
{
  "registry-mirrors": [
    "https://docker.mirrors.ustc.edu.cn",
    "https://hub-mirror.c.163.com",
    "https://mirror.baidubce.com"
  ],
  "max-concurrent-downloads": 10,
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
EOF

echo "✅ 配置已创建"
echo ""

# 重启 Docker 服务
echo "重启 Docker 服务..."
sudo systemctl daemon-reload
sudo systemctl restart docker

echo ""
echo "✅ Docker 镜像加速器配置完成！"
echo ""
echo "验证配置:"
echo "  docker info | grep -A 10 'Registry Mirrors'"
echo ""
