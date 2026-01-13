# 数据存储说明

## 目录结构

在 k3d 环境中，`data/minio_data` 和 `data/redis_data` 这两个目录**仍然是实际存储数据的地方**，但存储方式与之前版本有所不同。

## 存储机制

### k3d 环境中的存储流程

```
宿主机 (Ubuntu)
  └── /home/jack/work/DataAlchemy/data/
      ├── minio_data/          ← 实际数据存储在这里
      └── redis_data/          ← 实际数据存储在这里
           │
           │ (k3d volume mount)
           ▼
k3d 节点容器
  └── /data/
      ├── minio_data/          ← 映射到宿主机
      └── redis_data/          ← 映射到宿主机
           │
           │ (Kubernetes hostPath volume)
           ▼
Kubernetes Pod (MinIO/Redis)
  └── /data/                   ← 容器内的挂载点
      └── (实际数据)
```

### 数据流向

1. **k3d 集群创建时**：
   ```bash
   k3d cluster create dataalchemy \
       --volume "$PROJECT_ROOT/data:/data@all"
   ```
   这会将宿主机的 `$PROJECT_ROOT/data` 目录挂载到 k3d 节点容器的 `/data`

2. **Operator 部署时**：
   - Operator 检测到 k3d 环境，使用 `/data` 作为基础路径
   - 创建 Pod 时，使用 `hostPath` volume：
     ```yaml
     volumes:
     - name: minio-storage
       hostPath:
         path: "/data/minio_data"  # k3d 节点容器内的路径
     ```

3. **实际存储位置**：
   - 数据最终存储在：`/home/jack/work/DataAlchemy/data/minio_data/`
   - 数据最终存储在：`/home/jack/work/DataAlchemy/data/redis_data/`

## 与之前版本的区别

### Windows 版本（之前）
- 直接使用 `data/minio_data` 和 `data/redis_data`
- Docker Desktop 通过文件共享直接访问这些目录

### k3d 版本（现在）
- 仍然使用 `data/minio_data` 和 `data/redis_data`
- 但通过 k3d volume mount 和 Kubernetes hostPath 双重映射
- 数据持久化在宿主机，即使删除 k3d 集群也不会丢失

## 验证数据存储

### 检查宿主机数据
```bash
# 查看 MinIO 数据
ls -la data/minio_data/

# 查看 Redis 数据
ls -la data/redis_data/

# 查看数据大小
du -sh data/minio_data data/redis_data
```

### 检查容器内数据
```bash
# 查看 MinIO Pod 内的数据
kubectl exec -it $(kubectl get pod -l app=minio -o jsonpath='{.items[0].metadata.name}') -- ls -la /data

# 查看 Redis Pod 内的数据
kubectl exec -it $(kubectl get pod -l app=redis -o jsonpath='{.items[0].metadata.name}') -- ls -la /data
```

### 检查 k3d 节点容器内的数据
```bash
# 查看 k3d 节点容器内的挂载
docker exec k3d-dataalchemy-server-0 ls -la /data/
```

## 数据持久化

### 数据不会丢失的情况
- ✅ 删除并重新创建 Pod
- ✅ 重启 k3d 集群
- ✅ 更新 Operator 或 Stack

### 数据会丢失的情况
- ❌ 删除 k3d 集群（`k3d cluster delete dataalchemy`）
- ❌ 删除宿主机的 `data/minio_data` 或 `data/redis_data` 目录
- ❌ 使用 `--cleanup` 选项并选择删除数据

## 备份建议

### 备份数据
```bash
# 备份 MinIO 数据
tar -czf minio_backup_$(date +%Y%m%d).tar.gz data/minio_data/

# 备份 Redis 数据
tar -czf redis_backup_$(date +%Y%m%d).tar.gz data/redis_data/
```

### 恢复数据
```bash
# 恢复 MinIO 数据
tar -xzf minio_backup_YYYYMMDD.tar.gz

# 恢复 Redis 数据
tar -xzf redis_backup_YYYYMMDD.tar.gz
```

## 重要说明

### 当前配置问题

目前 Stack 配置中**没有显式设置 `dataPath`**，导致 operator 使用了错误的路径解析逻辑。数据实际存储在 k3d 节点容器内的 `/root/work/DataAlchemy/data/minio_data`，而不是直接映射到宿主机的 `data/minio_data`。

### 解决方案

在 `deploy/examples/stack-instance-k3d.yaml` 中显式设置 `dataPath: "/data"`：

```yaml
spec:
  storage:
    replicas: 1
    dataPath: "/data"  # k3d volume mount path
```

这样数据会正确存储在：
- k3d 节点容器内：`/data/minio_data` 和 `/data/redis_data`
- 宿主机：`$PROJECT_ROOT/data/minio_data` 和 `$PROJECT_ROOT/data/redis_data`

### 验证数据位置

```bash
# 检查 k3d 节点容器内的数据（应该在这里）
docker exec k3d-dataalchemy-server-0 ls -la /data/minio_data/

# 检查宿主机数据（k3d volume mount 后应该同步）
ls -la data/minio_data/
```

## 总结

**`data/minio_data` 和 `data/redis_data` 仍然是实际存储数据的地方**，通过 k3d 的 volume mount 机制实现数据持久化。这种设计的好处是：

1. ✅ **数据持久化**：数据存储在宿主机，不依赖容器生命周期
2. ✅ **易于备份**：直接备份宿主机目录即可
3. ✅ **跨平台兼容**：与 Windows 版本的数据结构保持一致
4. ✅ **便于迁移**：可以轻松迁移到其他 Kubernetes 环境

**注意**：确保在 Stack 配置中设置 `dataPath: "/data"` 以确保数据正确映射到宿主机。

## 相关配置

### k3d 集群配置
- 位置：`scripts/setup_k3d.sh`
- 关键配置：`--volume "$PROJECT_ROOT/data:/data@all"`

### Operator 配置
- 位置：`deploy/operator/main.py`
- 函数：`resolve_data_path()`
- 默认路径：`/data`（k3d 环境）

### Stack 配置
- 位置：`deploy/examples/stack-instance-k3d.yaml`
- 可以显式设置：`dataPath: "/data"`
