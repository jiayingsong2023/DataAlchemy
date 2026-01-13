#!/bin/bash
# 修复 GPU 权限问题

set -e

echo "=========================================="
echo "GPU 权限修复脚本"
echo "=========================================="
echo ""

echo "当前用户组:"
groups

echo ""
echo "步骤 1: 添加用户到 render 和 video 组..."
sudo usermod -a -G render,video $USER

echo ""
echo "✅ 用户已添加到 render 和 video 组"
echo ""
echo "⚠️  重要: 需要重新登录才能使组权限生效！"
echo ""
echo "请执行以下操作之一："
echo "1. 退出当前终端并重新打开"
echo "2. 运行: newgrp render"
echo "3. 或者重启系统"
echo ""
echo "重新登录后，运行以下命令验证："
echo "  groups  # 应该看到 render 和 video"
echo "  /opt/rocm/bin/rocminfo  # 应该不再有权限错误"
echo "  uv run python scripts/core/test_gpu.py  # 应该检测到 GPU"
