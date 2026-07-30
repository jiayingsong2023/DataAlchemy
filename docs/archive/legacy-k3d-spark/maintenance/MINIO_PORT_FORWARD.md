# MinIO Port-Forward 配置指南

## 问题

在 Windows 平台或某些 Kubernetes 环境中，MinIO 服务可能无法直接通过 `localhost:9000` 访问，需要使用 `kubectl port-forward` 来转发端口。

## 解决方案

### 方法 1: 自动 Port-Forward（推荐）

`scripts/manage_minio.py` 脚本现在会自动检测 MinIO 是否可用，如果不可用会自动设置 port-forward。

```bash
# 脚本会自动检测并设置 port-forward
uv run python scripts/manage_minio.py upload
```

### 方法 2: 手动 Port-Forward

如果需要手动管理 port-forward，可以使用辅助脚本：

```bash
# 在单独的终端中运行
./scripts/port_forward_minio.sh

# 或者指定服务名和命名空间
./scripts/port_forward_minio.sh dataalchemy-minio default 9000 9000
```

### 方法 3: 直接使用 kubectl

```bash
# 查找 MinIO 服务
kubectl get svc -A | grep minio

# 设置 port-forward（在后台运行）
kubectl port-forward svc/dataalchemy-minio 9000:9000 &

# 或者在前台运行（按 Ctrl+C 停止）
kubectl port-forward svc/dataalchemy-minio 9000:9000
```

## 环境配置

确保 `.env` 文件中的 `S3_ENDPOINT` 指向正确的地址：

```env
# 使用 localhost（port-forward 后）
S3_ENDPOINT=http://localhost:9000

# 或者使用服务名（仅在集群内部）
S3_ENDPOINT=http://dataalchemy-minio:9000
```

## 验证连接

测试 MinIO 是否可访问：

```bash
# 方法 1: 使用脚本检查
python3 -c "from scripts.manage_minio import check_minio_available; print(check_minio_available('http://localhost:9000'))"

# 方法 2: 使用 curl
curl -I http://localhost:9000/minio/health/live

# 方法 3: 使用脚本的 check 命令
uv run python scripts/manage_minio.py check
```

## 常见问题

### Q: 脚本提示 "MinIO is not accessible"

A: 这通常意味着：
1. MinIO 服务未运行：检查 `kubectl get pods -l app=minio`
2. Port-forward 未设置：运行 `./scripts/port_forward_minio.sh`
3. 端口被占用：检查 `netstat -an | grep 9000` 或 `lsof -i :9000`

### Q: 如何跳过自动 port-forward？

A: 使用 `--no-port-forward` 参数：

```bash
uv run python scripts/manage_minio.py upload --no-port-forward
```

### Q: Port-forward 在后台运行后如何停止？

A: 查找并终止进程：

```bash
# 查找 port-forward 进程
ps aux | grep "kubectl port-forward"

# 终止进程（替换 PID）
kill <PID>

# 或者使用 pkill
pkill -f "kubectl port-forward.*minio"
```

### Q: k3d 环境中还需要 port-forward 吗？

A: 在 k3d 中，如果配置了端口映射（如 `setup_k3d.sh` 中的 `--port "9000:30000"`），通常不需要 port-forward。但如果端口映射未正确配置，仍需要使用 port-forward。

## 更新后的脚本特性

`scripts/manage_minio.py` 现在包含：

1. ✅ **自动检测 MinIO 可用性**：检查 `localhost:9000` 是否可访问
2. ✅ **自动设置 port-forward**：如果不可用，自动查找 MinIO 服务并设置 port-forward
3. ✅ **自动清理**：脚本退出时自动清理 port-forward 进程
4. ✅ **手动控制选项**：可以使用 `--no-port-forward` 跳过自动设置

## 使用示例

```bash
# 自动模式（推荐）
uv run python scripts/manage_minio.py upload

# 手动 port-forward（在另一个终端）
./scripts/port_forward_minio.sh

# 然后运行脚本（跳过自动 port-forward）
uv run python scripts/manage_minio.py upload --no-port-forward
```
