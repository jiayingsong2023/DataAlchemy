# Ubuntu/k3d 测试计划

## 前置条件检查

### 1. 安装必要的工具

运行安装脚本：

```bash
./scripts/install_k8s_tools.sh
```

或者手动安装：

**Docker:**
```bash
sudo apt update
sudo apt install -y docker.io
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker $USER
# 重新登录或运行: newgrp docker
```

**kubectl:**
```bash
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
```

**k3d:**
```bash
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
```

### 2. 验证安装

```bash
docker --version
kubectl version --client
k3d version
```

---

## 测试步骤

### 测试 1: 创建 k3d 集群

```bash
cd /home/jack/work/DataAlchemy
./scripts/setup_k3d.sh
```

**预期结果:**
- ✅ k3d 集群创建成功
- ✅ kubectl 可以连接到集群
- ✅ 节点状态为 Ready

**验证命令:**
```bash
k3d cluster list
kubectl get nodes
kubectl cluster-info
```

---

### 测试 2: 部署 Operator

```bash
./scripts/setup_operator.sh
```

**预期结果:**
- ✅ Docker 镜像构建成功
- ✅ CRD 创建成功
- ✅ Operator pod 运行中
- ✅ RBAC 配置正确

**验证命令:**
```bash
kubectl get crd | grep dataalchemy
kubectl get pods -l app=dataalchemy-operator
kubectl logs -l app=dataalchemy-operator --tail=50
```

---

### 测试 3: 部署 Stack (k3d 配置)

```bash
kubectl apply -f deploy/examples/stack-instance-k3d.yaml
```

**预期结果:**
- ✅ DataAlchemyStack 资源创建成功
- ✅ MinIO deployment 运行中
- ✅ Redis deployment 运行中
- ✅ Services 创建成功（NodePort 类型）

**验证命令:**
```bash
kubectl get das
kubectl get pods -l stack=dataalchemy
kubectl get svc -l stack=dataalchemy
```

---

### 测试 4: 验证数据持久化

```bash
# 检查 hostPath 挂载
kubectl describe pod -l app=redis,stack=dataalchemy | grep -A 5 "Mounts:"
kubectl describe pod -l app=minio,stack=dataalchemy | grep -A 5 "Mounts:"

# 检查数据目录
ls -la data/redis_data data/minio_data 2>/dev/null || echo "数据目录检查..."
```

**预期结果:**
- ✅ Pod 正确挂载了 hostPath
- ✅ 数据目录存在且可访问

---

### 测试 5: 验证服务连接

```bash
# 检查服务端口
kubectl get svc -l stack=dataalchemy

# 测试 MinIO (应该通过 localhost:9000 访问)
curl -I http://localhost:9000/minio/health/live 2>/dev/null || echo "MinIO 连接测试..."

# 测试 Redis (应该通过 localhost:6379 访问)
redis-cli -h localhost -p 6379 ping 2>/dev/null || echo "Redis 连接测试..."
```

**预期结果:**
- ✅ MinIO 可通过 localhost:9000 访问
- ✅ Redis 可通过 localhost:6379 访问
- ✅ NodePort 映射正确

---

### 测试 6: 端到端测试

```bash
# 1. 初始化 MinIO 数据
uv run python scripts/manage_minio.py upload

# 2. 触发 Spark 作业（如果配置了）
# kubectl annotate das dataalchemy dataalchemy.io/request-ingest=$(date +%s)

# 3. 检查作业状态
kubectl get jobs -l stack=dataalchemy
```

**预期结果:**
- ✅ 数据上传成功
- ✅ Spark 作业可以执行（如果触发）
- ✅ 数据在 S3 中可见

---

## 故障排除

### 问题 1: Pod 无法启动

**检查:**
```bash
kubectl describe pod <pod-name>
kubectl logs <pod-name>
```

**常见原因:**
- hostPath 路径不存在或权限问题
- 镜像拉取失败
- 资源不足

### 问题 2: 服务无法访问

**检查:**
```bash
kubectl get svc -l stack=dataalchemy
kubectl get endpoints -l stack=dataalchemy
```

**常见原因:**
- Pod 未运行
- 端口映射配置错误
- k3d 端口映射未正确设置

### 问题 3: 数据持久化失败

**检查:**
```bash
kubectl describe pod -l app=redis | grep -A 10 "Volumes:"
ls -la data/
```

**常见原因:**
- hostPath 路径不存在
- 权限问题
- k3d volume 映射未配置

---

## 测试检查清单

- [ ] k3d 集群创建成功
- [ ] Operator 部署成功
- [ ] Stack 部署成功
- [ ] MinIO pod 运行中
- [ ] Redis pod 运行中
- [ ] Services 类型为 NodePort
- [ ] 服务可通过 localhost 访问
- [ ] 数据目录挂载正确
- [ ] 数据持久化工作正常

---

## 下一步

测试通过后：
1. 更新 README.md 添加 Ubuntu/k3d 部署说明
2. 记录任何发现的问题和解决方案
3. 更新文档中的故障排除部分
