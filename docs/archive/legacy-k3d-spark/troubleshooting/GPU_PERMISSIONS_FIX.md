# GPU 权限修复 - 重启 vs 重新登录

## 当前状态

用户需要添加到 `render` 和 `video` 组才能访问 GPU 设备。

## 解决方案

### 方法 1: 重新登录（推荐，更快）

组权限更改后，**重新登录**通常就足够了：

1. **添加用户到组**（如果还没有）:
   ```bash
   sudo usermod -a -G render,video $USER
   ```

2. **退出当前终端并重新打开**，或者：
   ```bash
   # 在当前终端立即生效（临时）
   newgrp render
   ```

3. **验证**:
   ```bash
   groups  # 应该看到 render 和 video
   /opt/rocm/bin/rocminfo  # 应该不再有权限错误
   ```

### 方法 2: 重启系统（最彻底）

如果重新登录后仍然有问题，可以重启系统：

```bash
sudo reboot
```

重启后，组权限会完全生效。

## 为什么需要重新登录/重启？

- `usermod` 命令修改的是 `/etc/group` 文件
- 当前会话的组信息在登录时加载
- 需要重新登录才能重新加载组信息

## 快速验证

重新登录或重启后，运行：

```bash
cd /home/jack/work/DataAlchemy

# 检查组
groups | grep -E "render|video"

# 测试 GPU
uv run python scripts/test_gpu.py
```

应该看到：
- ✅ GPU Available: True
- ✅ GPU Device Count: 1

## 如果仍然不工作

检查设备权限：
```bash
ls -la /dev/kfd /dev/dri/renderD*
```

应该看到 `render` 组有读写权限。
