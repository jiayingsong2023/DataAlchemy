# 清理旧数据指南

## 数据位置总结

### 1. 错误路径的数据（需要清理）

**位置**：k3d 节点容器内的 `/root/work/DataAlchemy/data/`
- MinIO 数据：`/root/work/DataAlchemy/data/minio_data` (约 632K，41 个文件)
- Redis 数据：`/root/work/DataAlchemy/data/redis_data`

这是之前 operator 使用错误路径解析时存储的数据。

### 2. 正确路径的数据（当前使用）

**位置**：k3d 节点容器内的 `/data/`（映射到宿主机 `$PROJECT_ROOT/data/`）
- MinIO 数据：`/data/minio_data`（映射到宿主机 `data/minio_data`）
- Redis 数据：`/data/redis_data`（映射到宿主机 `data/redis_data`）

这是配置 `dataPath: "/data"` 后使用的正确路径。

### 3. 宿主机数据

**位置**：`/home/jack/work/DataAlchemy/data/`
- MinIO 数据：`data/minio_data`（当前基本为空）
- Redis 数据：`data/redis_data`（有一些数据，但可能有权限问题）

## 清理方法

### 方法 1: 使用清理脚本（推荐）

```bash
# 运行清理脚本
./scripts/cleanup_old_data.sh
```

脚本会：
1. 显示所有数据位置和大小
2. 询问确认后清理错误路径的数据
3. 可选清理宿主机数据
4. 可选清理 MinIO bucket 数据

### 方法 2: 手动清理

#### 清理 k3d 节点容器内的错误路径数据

```bash
# 清理 MinIO 数据
docker exec k3d-dataalchemy-server-0 rm -rf /root/work/DataAlchemy/data/minio_data/*

# 清理 Redis 数据
docker exec k3d-dataalchemy-server-0 rm -rf /root/work/DataAlchemy/data/redis_data/*
```

#### 清理宿主机数据（可选）

```bash
# 清理 MinIO 数据
rm -rf data/minio_data/*

# 清理 Redis 数据
rm -rf data/redis_data/*
```

#### 清理 MinIO bucket 数据（通过 API）

```bash
# 使用脚本检查并清理
uv run python scripts/manage_minio.py check

# 或者直接删除 bucket 内容（需要 MinIO client）
kubectl exec -it $(kubectl get pod -l app=minio -o jsonpath='{.items[0].metadata.name}') -- \
  mc rm -r --force myminio/lora-data/
```

## 清理后重新开始

### 1. 更新 Stack 配置

确保使用正确的 `dataPath`：

```bash
# 应用更新后的配置
kubectl apply -f deploy/examples/stack-instance-k3d.yaml
```

### 2. 触发 Operator 重新协调

```bash
# 强制重新协调
kubectl annotate das dataalchemy dataalchemy.io/force-reconcile=$(date +%s) --overwrite
```

### 3. 验证数据路径

```bash
# 检查 Pod 使用的路径
kubectl get pod -l app=minio -o jsonpath='{.items[0].spec.volumes[0].hostPath.path}'
# 应该显示: /data/minio_data

# 检查 k3d 节点容器内的数据
docker exec k3d-dataalchemy-server-0 ls -la /data/minio_data/
```

### 4. 重新上传数据

```bash
# 上传数据到 MinIO
uv run python scripts/manage_minio.py upload

# 验证数据
uv run python scripts/manage_minio.py check
```

## 验证清理结果

### 检查错误路径是否已清理

```bash
# 检查 k3d 节点容器内的错误路径
docker exec k3d-dataalchemy-server-0 ls -la /root/work/DataAlchemy/data/minio_data/
# 应该只显示 . 和 .. 目录

# 检查数据大小
docker exec k3d-dataalchemy-server-0 du -sh /root/work/DataAlchemy/data/minio_data
# 应该显示 4.0K 或更小
```

### 检查正确路径的数据

```bash
# 检查 k3d 节点容器内的正确路径
docker exec k3d-dataalchemy-server-0 ls -la /data/minio_data/

# 检查宿主机数据
ls -la data/minio_data/
```

## 注意事项

1. **备份重要数据**：清理前请确保已备份重要数据
2. **停止相关服务**：清理 MinIO 数据前，建议先停止相关 Pod 或确保没有正在运行的作业
3. **权限问题**：如果遇到权限问题，可能需要使用 `sudo` 或调整目录权限
4. **Redis 数据**：Redis 数据可能包含缓存，清理后需要重新生成

## 完全清理（包括 k3d 集群）

如果需要完全重新开始：

```bash
# 1. 清理所有数据
./scripts/cleanup_old_data.sh

# 2. 删除 k3d 集群（可选）
k3d cluster delete dataalchemy

# 3. 重新创建集群
./scripts/setup_k3d.sh

# 4. 重新部署 operator
./scripts/setup_operator.sh
```

## 总结

- ✅ **可以清理干净**：所有旧数据都可以清理
- ✅ **使用清理脚本**：推荐使用 `./scripts/cleanup_old_data.sh`
- ✅ **更新配置**：清理后确保使用正确的 `dataPath: "/data"`
- ✅ **重新上传**：清理后需要重新上传数据
