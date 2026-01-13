# AMD AI Max+395 ROCm 安装指南

## 系统信息

- **CPU**: AMD RYZEN AI MAX+ 395 w/ Radeon 8060S
- **GPU 设备**: AMD/ATI Device 1586 (rev c1)
- **操作系统**: Ubuntu 24.04.3 LTS
- **状态**: amdgpu 内核驱动已加载 ✅，ROCm 运行时未安装 ❌

---

## 安装步骤

### 步骤 1: 更新系统并安装依赖

```bash
sudo apt update
sudo apt upgrade -y

# 安装必要的依赖
sudo apt install -y \
    wget \
    gnupg2 \
    software-properties-common \
    clinfo \
    rocm-dev \
    rocm-libs
```

### 步骤 2: 添加 AMD ROCm 官方仓库

```bash
# 添加 AMD ROCm GPG 密钥
wget -qO - https://repo.radeon.com/rocm/rocm.gpg.key | sudo apt-key add -

# 添加 ROCm 仓库（Ubuntu 24.04）
echo 'deb [arch=amd64] https://repo.radeon.com/rocm/apt/7.1/ jammy main' | sudo tee /etc/apt/sources.list.d/rocm.list

# 更新包列表
sudo apt update
```

### 步骤 3: 安装 ROCm 运行时和开发工具

```bash
# 安装 ROCm 核心组件
sudo apt install -y \
    rocm-dkms \
    rocm-dev \
    rocm-libs \
    rocm-utils \
    rocminfo \
    rocm-smi

# 安装 ROCm 开发工具（可选，用于编译）
sudo apt install -y \
    rocm-device-libs \
    rocblas \
    rocfft \
    rocrand \
    rocsparse \
    rocprim \
    hipblas \
    hipfft \
    hiprand \
    hipsparse
```

### 步骤 4: 配置用户权限

```bash
# 将当前用户添加到 render 和 video 组（如果还没有）
sudo usermod -a -G render,video $USER

# 注意：需要重新登录或重启才能生效
echo "⚠️  请重新登录或重启系统以使权限生效"
```

### 步骤 5: 验证安装

```bash
# 检查 ROCm 信息
rocminfo

# 检查 GPU 状态
rocm-smi

# 检查 OpenCL 设备
clinfo | grep -A 10 "Device Name"
```

### 步骤 6: 设置环境变量（可选但推荐）

将以下内容添加到 `~/.bashrc` 或 `~/.zshrc`:

```bash
# ROCm 环境变量
export ROCM_PATH=/opt/rocm
export PATH=$ROCM_PATH/bin:$PATH
export LD_LIBRARY_PATH=$ROCM_PATH/lib:$LD_LIBRARY_PATH

# HIP 环境变量
export HIP_PATH=$ROCM_PATH
export HIP_PLATFORM=amd
```

然后重新加载配置：
```bash
source ~/.bashrc  # 或 source ~/.zshrc
```

---

## 针对 AMD AI Max+395 的特殊说明

### 已知问题

根据 GitHub 社区反馈，AMD AI Max+395 在 Ubuntu 24.04 上可能存在以下问题：

1. **固件缺失警告**: 某些情况下可能提示缺少固件，但通常不影响基本功能
2. **ROCm 支持**: 需要 ROCm 7.1+ 版本才能完全支持

### 故障排除

如果遇到问题，尝试以下步骤：

```bash
# 1. 检查内核模块
lsmod | grep amdgpu

# 2. 检查设备文件
ls -la /dev/kfd
ls -la /dev/dri/

# 3. 检查 ROCm 版本
dpkg -l | grep rocm

# 4. 查看系统日志
sudo dmesg | grep -i amdgpu | tail -20
journalctl -k | grep -i amdgpu | tail -20
```

### 如果 rocm-smi 显示 "No devices found"

这可能是因为：
1. 需要重新登录以应用用户组权限
2. 需要重启系统
3. 内核模块未正确加载

尝试：
```bash
# 重新加载内核模块
sudo modprobe -r amdgpu
sudo modprobe amdgpu

# 检查设备权限
ls -la /dev/kfd
ls -la /dev/dri/renderD*
```

---

## 安装 uv（Python 包管理器）

项目使用 `uv` 来管理 Python 依赖，需要先安装：

```bash
# 使用一键安装脚本
cd /home/jack/work/DataAlchemy
./scripts/install_uv.sh

# 或者手动安装
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.cargo/bin:$PATH"
```

验证安装：
```bash
uv --version
```

## 安装 PyTorch with ROCm 7.1（已配置）

✅ **已配置**: `pyproject.toml` 已配置为使用 AMD 官方 ROCm 7.1 wheels，支持 AI Max+395。

### 安装步骤

```bash
cd /home/jack/work/DataAlchemy

# 同步所有依赖（包括 ROCm 7.1 版本的 PyTorch）
uv sync
```

### 配置说明

`pyproject.toml` 已配置为：
- **Linux**: 使用 AMD 官方 ROCm 7.1 wheels（来自 `https://repo.radeon.com/rocm/manylinux/rocm-rel-7.1/`）
  - PyTorch 2.9.0 + ROCm 7.1.0
  - 支持 AMD AI Max+395
- **其他平台**: 使用 CPU 版本

### 验证安装

安装完成后，运行：

```bash
uv run python scripts/test_gpu.py
```

应该看到 GPU 被正确识别。

---

## 验证 GPU 是否被 PyTorch 识别

运行项目中的 GPU 检测脚本（使用 uv）：

```bash
cd /home/jack/work/DataAlchemy

# 使用 uv 运行 GPU 检测脚本
uv run python scripts/test_gpu.py
```

如果一切正常，应该看到：
- ✅ GPU Available: True
- ✅ GPU Device Count: 1
- ✅ GPU Name: 显示你的 GPU 名称
- ✅ GPU computation successful

---

## 快速安装脚本

你可以使用以下一键安装脚本（需要 sudo 权限）：

```bash
#!/bin/bash
set -e

echo "🚀 开始安装 ROCm for AMD AI Max+395..."

# 更新系统
sudo apt update

# 安装依赖
sudo apt install -y wget gnupg2 software-properties-common

# 添加 ROCm 仓库
wget -qO - https://repo.radeon.com/rocm/rocm.gpg.key | sudo apt-key add -
echo 'deb [arch=amd64] https://repo.radeon.com/apt/7.1/ jammy main' | sudo tee /etc/apt/sources.list.d/rocm.list

# 更新并安装
sudo apt update
sudo apt install -y rocm-dkms rocm-dev rocm-libs rocm-utils rocminfo rocm-smi

# 添加用户到组
sudo usermod -a -G render,video $USER

echo "✅ ROCm 安装完成！"
echo "⚠️  请重新登录或重启系统，然后运行: rocm-smi"
```

---

## 参考链接

- [AMD ROCm 官方文档](https://rocm.docs.amd.com/)
- [ROCm 安装指南](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/)
- [AMD AI Max+395 驱动下载](https://www.amd.com/zh-cn/support/downloads/drivers.html/processors/ryzen-pro/ryzen-ai-max-pro-300-series/amd-ryzen-ai-max-plus-pro-395.html)
- [GitHub Issue: ROCm on AI Max+395](https://github.com/ROCm/ROCm/issues/4992)

---

**安装完成后，请运行 `python3 scripts/test_gpu.py` 验证 GPU 是否被正确识别！**
