#!/bin/bash
# uv 安装脚本
# 快速安装 uv Python 包管理器

set -e

echo "=========================================="
echo "uv Python 包管理器安装脚本"
echo "=========================================="
echo ""

# 检查是否已安装
if command -v uv &> /dev/null; then
    echo "✅ uv 已经安装"
    uv --version
    echo ""
    echo "如果需要重新安装，请先卸载: rm ~/.cargo/bin/uv"
    exit 0
fi

echo "正在安装 uv..."
echo ""

# 使用官方安装脚本
curl -LsSf https://astral.sh/uv/install.sh | sh

# 检查安装是否成功
if [ -f "$HOME/.cargo/bin/uv" ]; then
    # 添加到 PATH（如果还没有）
    if [[ ":$PATH:" != *":$HOME/.cargo/bin:"* ]]; then
        echo ""
        echo "📝 将 uv 添加到 PATH..."
        echo 'export PATH="$HOME/.cargo/bin:$PATH"' >> ~/.bashrc
        export PATH="$HOME/.cargo/bin:$PATH"
        echo "✅ 已添加到 ~/.bashrc"
        echo "⚠️  请运行: source ~/.bashrc 或重新打开终端"
    fi
    
    echo ""
    echo "=========================================="
    echo "✅ uv 安装成功！"
    echo "=========================================="
    echo ""
    "$HOME/.cargo/bin/uv" --version
    echo ""
    echo "下一步:"
    echo "1. 如果 PATH 已更新，运行: source ~/.bashrc"
    echo "2. 验证安装: uv --version"
    echo "3. 同步项目依赖: uv sync"
else
    echo "❌ 安装失败，请检查网络连接或手动安装"
    echo "   参考: https://github.com/astral-sh/uv"
    exit 1
fi
