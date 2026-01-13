# PyTorch ROCm 7.1 安装指南（AI Max+395）

## 问题说明

- **ROCm 6.4**: PyTorch 官方提供的 ROCm 6.4 wheels **不支持** AMD AI Max+395
- **ROCm 7.1**: 你的系统需要 ROCm 7.1+ 才能支持 AI Max+395
- **现状**: PyTorch 官方尚未提供 ROCm 7.1 的预编译 wheels

## 解决方案

### 方案 1: 使用 AMD Docker 镜像（推荐）

AMD 提供了预构建的 PyTorch Docker 镜像，包含 ROCm 7.1 支持：

```bash
# 拉取 PyTorch ROCm 7.1 Docker 镜像
docker pull rocm/pytorch:rocm7.1_ubuntu24.04_py3.12_pytorch_release_2.8.0

# 运行容器
docker run -it \
  --device=/dev/kfd \
  --device=/dev/dri \
  --group-add video \
  --ipc=host \
  --shm-size 8G \
  rocm/pytorch:rocm7.1_ubuntu24.04_py3.12_pytorch_release_2.8.0
```

### 方案 2: 从源码编译 PyTorch（高级）

如果需要在本地安装，可以从源码编译：

```bash
# 1. 克隆 PyTorch 仓库
git clone --recursive https://github.com/pytorch/pytorch
cd pytorch
git checkout v2.9.0  # 或最新版本

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置 ROCm 路径
export ROCM_PATH=/opt/rocm
export HIP_PATH=$ROCM_PATH

# 4. 编译（需要很长时间）
python setup.py install --cmake-only
USE_ROCM=1 python setup.py install
```

### 方案 3: 等待官方支持（临时使用 CPU）

如果暂时不需要 GPU 加速，可以：

1. 使用当前的 CPU 版本配置
2. 等待 PyTorch 官方发布 ROCm 7.1 wheels
3. 监控 PyTorch GitHub 和 AMD 官方公告

### 方案 4: 检查 AMD 官方仓库

定期检查 AMD 官方仓库是否有 Linux 版本的 PyTorch wheels：

```bash
# 检查 AMD 仓库
curl -s https://repo.radeon.com/rocm/manylinux/ | grep torch
```

## 当前配置说明

`pyproject.toml` 当前配置为使用 CPU 版本的 PyTorch，因为：

1. PyTorch 官方的 rocm6.4 不支持 AI Max+395
2. PyTorch 官方尚未提供 rocm7.1 wheels
3. 需要手动安装或使用 Docker

## 验证安装

安装完成后，运行：

```bash
uv run python scripts/test_gpu.py
```

应该看到 GPU 被正确识别。

## 参考链接

- [AMD ROCm 文档](https://rocm.docs.amd.com/)
- [PyTorch ROCm 支持](https://pytorch.org/get-started/locally/)
- [AMD Docker 镜像](https://hub.docker.com/r/rocm/pytorch)
- [PyTorch GitHub Issues - ROCm 7.1](https://github.com/pytorch/pytorch/issues)

## 更新日志

- 2025-01-XX: 确认 ROCm 6.4 不支持 AI Max+395，需要 ROCm 7.1+
- 待更新: PyTorch 官方发布 ROCm 7.1 wheels 后更新配置
