# PDF 到问答、记忆和 LoRA 的最小闭环

这份文档说明如何把一份 PDF 接入 DataAlchemy，并在 WebUI 中验证内容是否被利用。

先说明当前边界：

- PDF → raw → Spark rough clean → fine clean → PostgreSQL documents/chunks → RAG → WebUI 问答，当前可以通过单文件试点入口完成。
- 记忆写入来自**会话中的提炼结果**，不是把外部 PDF 原文直接写进长期记忆；低风险个人记忆可以自动批准，团队/租户记忆仍需审核。
- fine clean 产物是规范化 document/chunk，不是可直接训练的 SFT 数据；中间还需要 training-candidate builder。
- PDF 上传不会自动触发 LoRA。LoRA 还需要 H5 的标注、训练快照、GPU Job、固定评测和发布审批；当前没有“一次上传后 WebUI 自动训练 adapter”的入口。

## 1. 前置条件

### 软件与服务

- Linux（推荐 Ubuntu 22.04/24.04）、Docker、k3d、kubectl、Helm 3；
- Python 3.12、`uv`，并已创建项目 `.venv`；
- PostgreSQL 16 + pgvector、MinIO、Redis；
- Spark Job 镜像和 k3d 集群可用；PDF/RAG 可以 CPU 运行，LoRA adapter 训练还需要 AMD ROCm GPU Job 镜像；
- 若使用本地模型，`data/models/TinyLlama`、embedding/reranker 模型和 Spark JAR 必须已经准备好。

### 环境变量

在项目根目录准备 `.env`（不要提交到 Git），至少配置：

```bash
export DATABASE_URL='postgresql://dataalchemy_app:password@postgres-host:5432/dataalchemy'
export VERIFIER_DATABASE_URL='postgresql://dataalchemy_verifier:password@postgres-host:5432/dataalchemy'
export REDIS_URL='redis://redis-host:6379'
export S3_ENDPOINT='http://minio-host:9000'
export S3_BUCKET='data-alchemy'
export AUTH_SECRET_KEY='replace-with-a-unique-secret'
```

管理员身份必须包含 `tenant_id`；生产环境使用 OIDC，不能使用默认管理员密码。

## 2. 删除旧测试环境并重建

以下命令只适用于专用测试集群。执行前确认当前 context 和数据库不是生产环境；删除集群会同时删除其中的 Job、containerd 镜像缓存和 PVC。

### 2.1 只清空数据（推荐）

保留 k3d 集群和镜像，仅清空已注册测试环境的数据：

```bash
.venv/bin/python scripts/reset_pilot_environment.py \
  --environment dataalchemy-gpu-test
```

命令会打印 `plan_sha256`。确认目标确实是测试环境后再执行：

```bash
export PILOT_RESET_DATABASE_URL="$DATABASE_URL"
export PILOT_RESET_S3_ENDPOINT="$S3_ENDPOINT"
export PILOT_RESET_S3_ACCESS_KEY='admin'
export PILOT_RESET_S3_SECRET_KEY='minioadmin'
export PILOT_RESET_REDIS_URL="$REDIS_URL"

.venv/bin/python scripts/reset_pilot_environment.py \
  --environment dataalchemy-gpu-test --execute \
  --confirm 'reset:dataalchemy-gpu-test:<上一步输出的plan_sha256前12位>'
```

该流程只清理预注册的 PostgreSQL 测试表、MinIO `h6-test/` 前缀、Redis 测试前缀和测试 namespace Job，不接受生产/共享目标。

### 2.2 完整删除并重建 k3d 测试集群

```bash
export K3D_CLUSTER_NAME=dataalchemy-pdf-test

# 只对专用测试集群执行
helm uninstall data-alchemy --namespace data-alchemy --wait || true
k3d cluster delete "$K3D_CLUSTER_NAME" || true

K3D_CLUSTER_NAME="$K3D_CLUSTER_NAME" ./scripts/setup/setup_k3d.sh
k3d kubeconfig merge "$K3D_CLUSTER_NAME" --switch-context
kubectl get nodes
```

然后构建并导入应用镜像：

```bash
docker build -t data-alchemy:latest .
k3d image import data-alchemy:latest -c "$K3D_CLUSTER_NAME"
helm upgrade --install data-alchemy deploy/charts/data-alchemy \
  --namespace data-alchemy --create-namespace --wait --timeout 600s

.venv/bin/python scripts/migrate_postgres.py
.venv/bin/python scripts/pilot_check.py
kubectl get pods -n data-alchemy
```

上面的 `setup_k3d.sh` 创建的是普通测试集群。若要运行 LoRA GPU Job，必须使用已经配置 AMD CDI/ROCm 的专用 GPU 集群，并确认 H5 Job 镜像和 GPU preflight 通过；不要把普通 CPU 集群误宣称为 LoRA 环境。

## 3. 准备 PDF 和环境

PDF 必须是可复制文本、未加密、损坏检查通过，默认不超过 25 MiB。扫描件 OCR、复杂表格和密码保护 PDF 当前会被拒绝。

启动 PostgreSQL/pgvector、MinIO、Redis、WebUI 和 k3d Job 环境后，确认：

```bash
.venv/bin/python scripts/pilot_check.py
kubectl get pods -n data-alchemy
```

生产环境使用 OIDC；本地试点使用管理员账号即可。

## 4. PDF 入库：rough clean → fine clean → RAG

1. 登录 WebUI，进入 **Agent Tasks**，点击文件导入，选择 PDF。
2. 输入一个能验证文档内容的问题，例如：

   ```text
   这份文档的支持时间和 P1 处理要求是什么？
   ```

3. 按页面提示逐步批准当前 tenant、当前 PDF hash 对应的任务。
4. 在任务详情中等待以下五步全部通过：

   | 阶段 | 主要产物 |
   | --- | --- |
   | Input validation | PDF SHA-256、source version、ACL snapshot |
   | Spark rough clean | MinIO `cleaned_corpus`、accepted/quarantined/rejected 计数 |
   | Fine clean / refine | normalized corpus、页码 locator、PII/injection 检查 |
   | Publish | PostgreSQL `documents`、`document_chunks`、ACL、FTS/向量 |
   | RAG probe | 固定问题命中的 chunk、source version、PDF 页码引用 |

原始 PDF 只保存在受限 MinIO raw 区；检索权威是 PostgreSQL，不是 MinIO 文件本身。

## 5. 让会话内容进入 memory system

PDF 内容先通过 RAG 被回答，再从会话记录提炼记忆：

1. 创建聊天会话时开启 `auto_memory_enabled`，或在 WebUI 的会话设置中打开自动记忆。
2. 针对 PDF 提问并确认回答引用了正确页码。
3. 结束会话，或调用：

   ```bash
   curl -X POST "$WEBUI_URL/api/sessions/$SESSION_ID/distill" \
     -H "Authorization: Bearer $TOKEN"
   ```

4. 查看 `/api/memories?query=关键词` 或 WebUI 的 Memory 面板：
   - `approved`：可被后续上下文检索；
   - `candidate`：需要审核；
   - `conflicted`/`rejected`：不会作为有效记忆使用。

外部 PDF 默认是 `untrusted_external`。因此不要把 PDF 中的“请调用工具”或“写入长期记忆”等文字当作系统指令；这类内容会被清洗或隔离。

## 6. 用 PDF 相关数据训练 LoRA adapter

fine clean 之后先生成训练候选，而不是直接把所有 chunk 喂给模型：

```text
normalized chunks
→ instruction/input/output 或 conversations 候选
→ 绑定 source chunk、页码、ACL、hash
→ 人工审核
→ train/validation snapshot
```

使用最小的 `build_pdf_training_candidates.py` 完成这个转换。它只负责校验来源并生成带
`split`、hash、页码、ACL 和 tenant lineage 的候选 JSONL，不负责自动批准、创建 snapshot 或训练。
输入 QA JSONL 必须已经由人工或已校准 Judge 审核，并明确 `training_allowed: true`：

```jsonl
{"source_chunk_id":"chunk-001","split":"train","review_status":"approved","training_allowed":true,"permission_version":"pdf-v1","instruction":"概括本页。","input":"","output":"这里是经过审核的答案。"}
{"source_chunk_id":"chunk-002","split":"validation","review_status":"approved","training_allowed":true,"permission_version":"pdf-v1","instruction":"本节的结论是什么？","input":"","output":"这里是验证答案。"}
```

```bash
.venv/bin/python scripts/build_pdf_training_candidates.py \
  --corpus normalized_documents.json \
  --reviewed-qa reviewed-qa.jsonl \
  --output pdf-candidates.jsonl \
  --manifest pdf-candidates.manifest.json
```

然后把候选 JSONL 和 manifest 交给 H5 的 snapshot/evaluation 流程；这个脚本不能替代审核或发布门禁。

这一步目前仍是受控的 H5 运维流程，不是 PDF 上传任务的自动后续步骤：

1. 从已完成的 PDF 问答轨迹中选择训练样本和 validation 样本。
2. 人工审核 annotation，并明确 `training_allowed`、training purpose 和 permission version。
3. 创建并审核 `training_snapshot`。
4. 在 GPU Job 中执行 LoRA，生成 adapter manifest。
5. 用同一固定 evaluation suite 对 base/candidate 评测。
6. 通过 safety scan、独立 reviewer、shadow/canary 和 rollback 检查后，才允许发布 adapter。

当前没有实现上述候选构建器时，不要手工把 `cleaned_corpus` 或 PDF 原文改名为训练集；这会丢失监督标签和来源许可。

当前可参考 H5 工程演练：

```bash
.venv/bin/python scripts/run_h5_rehearsal.py
```

该脚本使用 synthetic 数据，只能验证 LoRA 工程链路，不能把本次 PDF 自动变成生产 adapter。实际 PDF LoRA 需要通过 H5 evaluation/snapshot/release API 或受控运维脚本接入，不能直接把 PDF 原文喂给训练程序。

## 7. 在 WebUI 验证最终问答

完成入库后，在聊天框重复第 2 步的问题。验收以下内容：

- 回答包含 PDF 的实际内容；
- Evidence 显示正确的 `document_id`、`chunk_id`、source version 和页码；
- `/api/sessions/{session_id}/context` 能看到相关 document chunk 和 approved memory；
- 反馈、会话、记忆和任务都属于当前 tenant；
- 如果 adapter 已通过 H5 发布，模型版本/adapter release 能在 H5 页面中查询，否则问答仍使用 base model + RAG。

## 8. 一句话结论

当前项目可以完整展示 **PDF → rough clean → fine clean → PostgreSQL RAG → WebUI 问答 → 会话记忆提炼**。但 **PDF → 自动 LoRA → adapter 发布** 仍是需要人工审核和 H5 评测门禁的独立流程，尚未提供单击自动闭环。
