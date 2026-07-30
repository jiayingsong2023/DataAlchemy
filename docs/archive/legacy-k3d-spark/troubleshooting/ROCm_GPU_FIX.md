# ROCm GPU 检测问题修复指南

## 问题症状

- ✅ ROCm 已安装（rocm-smi 可以工作）
- ✅ PyTorch ROCm 版本已安装
- ❌ `torch.cuda.is_available()` 返回 False
- ❌ `rocminfo` 显示权限错误

## 根本原因

用户不在 `render` 和 `video` 组中，无法访问 `/dev/kfd` 设备。

## 解决步骤

### 步骤 1: 添加用户到组（如果还没有）

```bash
sudo usermod -a -G render,video $USER
```

### 步骤 2: 重新登录或重启系统

**重要**: 组权限更改需要重新登录才能生效！

你可以：
- 退出当前终端会话并重新登录
- 或者重启系统

### 步骤 3: 验证组权限

重新登录后，运行：

```bash
groups
```

应该看到 `render` 和 `video` 在列表中。

### 步骤 4: 设置环境变量

环境变量已添加到 `~/.bashrc`，重新登录后会自动加载。或者手动加载：

```bash
source ~/.bashrc
```

### 步骤 5: 验证 ROCm

```bash
# 检查 ROCm 信息（应该不再有权限错误）
/opt/rocm/bin/rocminfo

# 检查 GPU
rocm-smi
```

### 步骤 6: 测试 PyTorch GPU

```bash
cd /home/jack/work/DataAlchemy
uv run python scripts/test_gpu.py
```

应该看到：
- ✅ GPU Available: True
- ✅ GPU Device Count: 1
- ✅ GPU Name: 显示你的 GPU 名称

## 如果仍然不工作

1. **检查设备权限**:
   ```bash
   ls -la /dev/kfd /dev/dri/renderD*
   ```

2. **检查环境变量**:
   ```bash
   echo $ROCM_PATH
   echo $HIP_PATH
   echo $LD_LIBRARY_PATH | grep rocm
   ```

3. **手动设置环境变量测试**:
   ```bash
   export ROCM_PATH=/opt/rocm
   export HIP_PATH=/opt/rocm
   export LD_LIBRARY_PATH=/opt/rocm/lib:$LD_LIBRARY_PATH
   uv run python -c "import torch; print(torch.cuda.is_available())"
   ```

4. **检查 PyTorch 版本**:
   ```bash
   uv run python -c "import torch; print(torch.__version__); print(torch.version.hip)"
   ```
   应该显示 ROCm 版本。

## 快速验证脚本

重新登录后，运行：

```bash
cd /home/jack/work/DataAlchemy
./scripts/test_gpu.py
```

如果一切正常，应该看到 GPU 被正确识别！
