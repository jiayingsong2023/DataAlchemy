# MinIO 数据迁移指南

## 当前情况

### 数据位置

- **Stack 配置**：已设置为 `dataPath: "/data"`（正确路径）
- **Pod 实际使用**：`/root/work/DataAlchemy/data/minio_data`（错误路径）
- **数据实际存储**：`/root/work/DataAlchemy/data/minio_data/lora-data/`（错误路径）
- **正确路径状态**：`/data/minio_data`（当前为空）

### 问题原因

Stack 配置已更新，但 Pod 还没有重新创建，所以仍在使用旧的错误路径。

## 解决方案

### 方案 1: 迁移数据到正确路径（推荐）

保留现有数据，迁移到正确路径：

```bash
# 1. 停止 MinIO Pod（可选，为了数据一致性）
kubectl scale deployment dataalchemy-minio --replicas=0

# 2. 在 k3d 节点容器内迁移数据
docker exec k3d-dataalchemy-server-0 bash -c "
  # 创建目标目录
  mkdir -p /data/minio_data
  
  # 复制数据（如果源目录存在且有数据）
  if [ -d /root/work/DataAlchemy/data/minio_data ] && [ \"\$(ls -A /root/work/DataAlchemy/data/minio_data)\" ]; then
    cp -r /root/work/DataAlchemy/data/minio_data/* /data/minio_data/
    echo 'Data migrated successfully'
  fi
"

# 3. 更新 Stack 配置（确保 dataPath 正确）
kubectl apply -f deploy/examples/stack-instance-k3d.yaml

# 4. 触发重新协调（这会重新创建 Pod 使用新路径）
kubectl annotate das dataalchemy dataalchemy.io/force-reconcile=$(date +%s) --overwrite

# 5. 等待 Pod 重新创建
kubectl wait --for=condition=ready pod -l app=minio --timeout=60s

# 6. 验证数据
uv run python scripts/manage_minio.py list
```

### 方案 2: 重新上传数据（简单但需要重新上传）

如果数据不重要或可以重新生成：

```bash
# 1. 清理旧数据
./scripts/cleanup_old_data.sh

# 2. 更新 Stack 配置
kubectl apply -f deploy/examples/stack-instance-k3d.yaml

# 3. 触发重新协调
kubectl annotate das dataalchemy dataalchemy.io/force-reconcile=$(date +%s) --overwrite

# 4. 等待 Pod 重新创建
kubectl wait --for=condition=ready pod -l app=minio --timeout=60s

# 5. 重新上传数据
uv run python scripts/manage_minio.py upload
```

### 方案 3: 使用 MinIO 客户端迁移（最安全）

通过 MinIO API 导出和导入数据：

```bash
# 1. 导出数据（从旧路径的 MinIO）
# 需要先设置 port-forward 到旧 Pod（如果还在运行）
kubectl port-forward svc/dataalchemy-minio 9000:9000 &
PF_PID=$!

# 2. 使用 mc (MinIO Client) 导出
# 安装 mc（如果还没有）
# 或者使用 Python 脚本导出

# 3. 停止旧 Pod
kubectl scale deployment dataalchemy-minio --replicas=0

# 4. 更新配置并重新创建 Pod
kubectl apply -f deploy/examples/stack-instance-k3d.yaml
kubectl annotate das dataalchemy dataalchemy.io/force-reconcile=$(date +%s) --overwrite

# 5. 等待新 Pod 启动
kubectl wait --for=condition=ready pod -l app=minio --timeout=60s

# 6. 导入数据到新 Pod
# 使用 Python 脚本或 mc 导入

# 7. 停止 port-forward
kill $PF_PID
```

## 推荐方案：方案 1（直接迁移）

最简单直接的方法：

```bash
#!/bin/bash
# 快速迁移脚本

echo "开始迁移 MinIO 数据..."

# 1. 检查源数据
echo "检查源数据..."
docker exec k3d-dataalchemy-server-0 ls -la /root/work/DataAlchemy/data/minio_data/lora-data/ 2>/dev/null || echo "源数据不存在"

# 2. 创建目标目录
echo "创建目标目录..."
docker exec k3d-dataalchemy-server-0 mkdir -p /data/minio_data

# 3. 迁移数据
echo "迁移数据..."
docker exec k3d-dataalchemy-server-0 bash -c "
  if [ -d /root/work/DataAlchemy/data/minio_data ] && [ \"\$(ls -A /root/work/DataAlchemy/data/minio_data 2>/dev/null)\" ]; then
    cp -a /root/work/DataAlchemy/data/minio_data/* /data/minio_data/ 2>/dev/null
    echo '数据迁移完成'
  else
    echo '源数据目录为空或不存在'
  fi
"

# 4. 验证迁移
echo "验证迁移结果..."
docker exec k3d-dataalchemy-server-0 ls -la /data/minio_data/lora-data/ 2>/dev/null || echo "目标目录为空"

# 5. 更新 Stack 配置
echo "更新 Stack 配置..."
kubectl apply -f deploy/examples/stack-instance-k3d.yaml

# 6. 触发重新协调
echo "触发重新协调..."
kubectl annotate das dataalchemy dataalchemy.io/force-reconcile=$(date +%s) --overwrite

# 7. 等待 Pod 重新创建
echo "等待 Pod 重新创建..."
kubectl wait --for=condition=ready pod -l app=minio --timeout=60s

# 8. 验证数据
echo "验证数据..."
sleep 5
uv run python scripts/manage_minio.py list

echo "迁移完成！"
```

## 验证迁移结果

### 检查 Pod 使用的路径

```bash
# 应该显示 /data/minio_data
kubectl get pod -l app=minio -o jsonpath='{.items[0].spec.volumes[0].hostPath.path}'
```

### 检查数据位置

```bash
# 检查 k3d 节点容器内的正确路径
docker exec k3d-dataalchemy-server-0 ls -la /data/minio_data/lora-data/

# 检查宿主机数据
ls -la data/minio_data/
```

### 验证 MinIO 数据

```bash
# 列出 MinIO bucket 内容
uv run python scripts/manage_minio.py list

# 应该能看到之前的数据
```

## 注意事项

1. **数据备份**：迁移前建议备份数据
2. **服务中断**：迁移过程中 MinIO 服务可能会短暂中断
3. **权限问题**：确保目录权限正确
4. **数据一致性**：迁移时确保没有正在运行的写入操作

## 清理旧数据

迁移成功后，可以清理旧路径的数据：

```bash
# 清理错误路径的数据
docker exec k3d-dataalchemy-server-0 rm -rf /root/work/DataAlchemy/data/minio_data/*
docker exec k3d-dataalchemy-server-0 rm -rf /root/work/DataAlchemy/data/redis_data/*
```

## 总结

- ✅ **数据可以迁移**：从错误路径迁移到正确路径
- ✅ **推荐方案 1**：直接复制数据最简单
- ✅ **验证很重要**：迁移后务必验证数据完整性
- ✅ **清理旧数据**：迁移成功后清理旧路径
