#!/bin/bash
# 配置 Docker 使用系统代理（适用于 FIClash/VPN 环境）

set -e

echo "=========================================="
echo "配置 Docker 使用代理"
echo "=========================================="
echo ""

# 检测代理设置
HTTP_PROXY="${HTTP_PROXY:-${http_proxy}}"
HTTPS_PROXY="${HTTPS_PROXY:-${https_proxy}}"

if [ -z "$HTTP_PROXY" ] && [ -z "$HTTPS_PROXY" ]; then
    echo "⚠️  未检测到系统代理设置"
    echo ""
    echo "请先设置环境变量，例如："
    echo "  export HTTP_PROXY=http://127.0.0.1:7890"
    echo "  export HTTPS_PROXY=http://127.0.0.1:7890"
    echo ""
    echo "或者如果使用 FIClash，通常代理地址是："
    echo "  http://127.0.0.1:7890 (HTTP)"
    echo "  http://127.0.0.1:7890 (HTTPS)"
    echo ""
    read -p "请输入 HTTP 代理地址 (例如 http://127.0.0.1:7890，直接回车跳过): " PROXY_INPUT
    
    if [ -n "$PROXY_INPUT" ]; then
        HTTP_PROXY="$PROXY_INPUT"
        HTTPS_PROXY="$PROXY_INPUT"
    else
        echo "❌ 未提供代理地址，退出"
        exit 1
    fi
fi

echo "使用代理:"
echo "  HTTP_PROXY: $HTTP_PROXY"
echo "  HTTPS_PROXY: $HTTPS_PROXY"
echo ""

# 创建 Docker daemon 配置目录
sudo mkdir -p /etc/docker

# 检查是否已有配置
if [ -f /etc/docker/daemon.json ]; then
    echo "⚠️  发现现有配置 /etc/docker/daemon.json"
    echo "备份为 /etc/docker/daemon.json.bak"
    sudo cp /etc/docker/daemon.json /etc/docker/daemon.json.bak
fi

# 创建新的配置
echo "创建 Docker 代理配置..."
sudo tee /etc/docker/daemon.json > /dev/null <<EOF
{
  "proxies": {
    "default": {
      "httpProxy": "$HTTP_PROXY",
      "httpsProxy": "$HTTPS_PROXY",
      "noProxy": "localhost,127.0.0.1,*.local"
    }
  }
}
EOF

echo "✅ 配置已创建"
echo ""

# 创建 systemd drop-in 目录
sudo mkdir -p /etc/systemd/system/docker.service.d

# 创建代理环境变量配置
echo "创建 Docker 服务代理配置..."
sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf > /dev/null <<EOF
[Service]
Environment="HTTP_PROXY=$HTTP_PROXY"
Environment="HTTPS_PROXY=$HTTPS_PROXY"
Environment="NO_PROXY=localhost,127.0.0.1,*.local"
EOF

echo "✅ 服务配置已创建"
echo ""

# 重启 Docker 服务
echo "重启 Docker 服务..."
sudo systemctl daemon-reload
sudo systemctl restart docker

echo ""
echo "✅ Docker 代理配置完成！"
echo ""
echo "验证配置:"
echo "  docker info | grep -i proxy"
echo ""
echo "测试连接:"
echo "  docker pull hello-world"
echo ""
