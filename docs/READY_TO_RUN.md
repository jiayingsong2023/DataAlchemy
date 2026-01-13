# 环境就绪检查清单

## ✅ 已完成的设置

### 1. 基础设施
- ✅ k3d 集群已创建并运行
- ✅ Operator 已部署
- ✅ MinIO 服务已部署（NodePort 30000）
- ✅ Redis 服务已部署（NodePort 30002）
- ✅ 数据已迁移到正确路径（`/data/minio_data`）
- ✅ MinIO 数据已上传（raw 数据可见）

### 2. 数据存储
- ✅ MinIO bucket `lora-data` 已创建
- ✅ 原始数据已上传到 `s3://lora-data/raw/`
- ✅ 数据持久化配置正确（映射到宿主机 `data/minio_data`）

### 3. 网络配置
- ✅ MinIO API: `http://localhost:9000`（通过 k3d port mapping）
- ✅ MinIO Console: `http://localhost:9001`
- ✅ Redis: `redis://localhost:6379`（通过 k3d port mapping）

## 🚀 可以执行的命令

### 数据摄取（Ingestion）

#### 1. 粗洗（Rough Cleaning）
```bash
uv run data-alchemy ingest --mode spark --stage wash
```
- 使用 Spark 进行数据清洗
- 生成 `cleaned_corpus.jsonl`、`rag_chunks.jsonl` 和 `metrics.parquet`

#### 2. 数值量化（Feature Engineering）
```bash
uv run data-alchemy quant --input data/processed/metrics.parquet --output data/processed/quant
```
- 使用 Polars Streaming 进行特征工程
- 处理百万级数据，内存占用小

#### 3. 精洗和索引（Refinement & Indexing）
```bash
uv run data-alchemy ingest --stage refine --synthesis --max_samples 50
```
- 将粗洗数据转换为 SFT 训练对
- 构建 FAISS 知识索引

#### 4. 完整摄取流程
```bash
uv run data-alchemy ingest --mode spark --synthesis --max_samples 50
```
- 一次性完成：粗洗 + 自动量化 + LLM 合成 + FAISS 索引

### 训练（Training）

```bash
uv run train-lora
```
- 使用精洗后的 SFT 数据微调模型
- 需要 GPU 支持（ROCm）

### 交互式聊天（Chat）

#### WebUI（推荐）
```bash
uv run python webui/app.py
```
- 启动 WebUI 服务器（HTTPS on 8443）
- 访问 `https://localhost:8443`
- 支持实时状态更新、流式响应、Redis 会话持久化

### 监控和基准测试

#### 查看实时指标
访问 `https://localhost:8443/metrics`（WebUI 运行时）

#### 性能基准测试
```bash
uv run python scripts/benchmark_inference.py --users 5 --reqs 10
```
- 模拟 5 个并发用户，每个用户 10 个请求

### 自动进化（Auto-Evolution）

#### 一次性完整循环
```bash
uv run schedule-sync full-cycle --mode spark --synthesis
```

#### 周期性调度
```bash
uv run schedule-sync schedule --mode spark --interval 24 --synthesis
```
- 每 24 小时自动运行一次完整流程

## ⚠️ 注意事项

### 1. 环境变量
确保 `.env` 文件包含必要的配置：
```env
# S3 / MinIO
S3_ENDPOINT=http://localhost:9000
S3_BUCKET=lora-data
AWS_ACCESS_KEY_ID=minioadmin
AWS_SECRET_ACCESS_KEY=minioadmin

# Redis
REDIS_URL=redis://localhost:6379

# DeepSeek API（用于 LLM 合成）
DEEPSEEK_API_KEY=your_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

### 2. GPU 支持
- 确保 ROCm 已正确安装
- 运行 `uv run python scripts/test_gpu.py` 验证 GPU 可用性
- 如果 GPU 不可用，某些操作（如训练）可能失败或使用 CPU

### 3. 数据路径
- 处理后的数据会存储在 `data/processed/`
- SFT 训练数据：`data/sft_train.jsonl`
- FAISS 索引：`data/faiss_index.bin`
- RAG chunks：`data/rag_chunks.jsonl`

### 4. Spark Jobs
- Spark 作业会在 Kubernetes 中运行
- 检查作业状态：`kubectl get jobs`
- 查看日志：`kubectl logs -l component=spark-ingest`

## 📋 快速开始示例

### 完整工作流程

```bash
# 1. 检查环境
uv run python scripts/test_gpu.py
uv run python scripts/manage_minio.py list

# 2. 运行完整摄取流程
uv run data-alchemy ingest --mode spark --synthesis --max_samples 50

# 3. 训练模型
uv run train-lora

# 4. 启动 WebUI
uv run python webui/app.py
# 然后在浏览器中访问 https://localhost:8443
```

## 🔍 故障排除

### MinIO 连接问题
```bash
# 检查服务状态
kubectl get svc -l stack=dataalchemy

# 检查 Pod 状态
kubectl get pods -l app=minio

# 测试连接
uv run python scripts/manage_minio.py check
```

### Redis 连接问题
```bash
# 检查服务状态
kubectl get svc dataalchemy-redis

# 测试连接
redis-cli -h localhost -p 6379 ping
```

### GPU 问题
```bash
# 运行 GPU 检测
uv run python scripts/test_gpu.py

# 检查 ROCm
rocm-smi
```

## ✅ 总结

**环境已就绪，可以开始执行 README.md 中的命令！**

建议从简单的命令开始：
1. 先运行 `uv run data-alchemy ingest --mode spark --stage wash` 测试数据摄取
2. 然后逐步运行更复杂的流程
3. 最后启动 WebUI 进行交互式测试
