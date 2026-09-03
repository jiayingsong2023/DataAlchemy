# PDF 到问答、记忆和 LoRA 的最小闭环

这份文档说明如何把一份 PDF 接入 DataAlchemy，并在 WebUI 中验证内容是否被利用。

先说明当前边界：

- PDF → raw → Spark rough clean → fine clean → PostgreSQL documents/chunks → RAG → WebUI 问答，当前可以通过单文件试点入口完成。
- 记忆写入来自**会话中的提炼结果**，不是把外部 PDF 原文直接写进长期记忆；低风险个人记忆可以自动批准，团队/租户记忆仍需审核。
- fine clean 产物是规范化 document/chunk，不是可直接训练的 SFT 数据；中间还需要 training-candidate builder。
- PDF 上传不会无门禁自动触发 LoRA。当前已有一个可恢复的两阶段 CLI 入口，
  但仍需要 WebUI 对话、反馈审核、训练快照、GPU Job、固定评测和发布审批。

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

本机 GPU 集群的推荐流程已经集中到
[LOCAL_ENVIRONMENT_OPERATIONS.md](./LOCAL_ENVIRONMENT_OPERATIONS.md)。该文档会使用当前
`setup_k3d.sh` 的 `dataalchemy-gpu` 默认值、导入独立的 Web/H5/ETL 镜像和基础镜像、
开启 H5 GPU Job 所需配置，并通过端口转发执行数据库迁移。以下仅保留 reset 语义和验收
边界，避免与部署脚本产生两套命令。

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

### 2.2 完整删除并重建

按 [LOCAL_ENVIRONMENT_OPERATIONS.md](./LOCAL_ENVIRONMENT_OPERATIONS.md) 的第 1--5 节执行。
当前脚本默认创建 GPU-enabled `dataalchemy-gpu`；普通 CPU 集群不能用来宣称 LoRA GPU Job
通过。H5 的 `data-alchemy:h5-canonical-local` 只用于本地 cache-backed 验证，且不能
兼任 Web 或 ETL 镜像，也不能替代 H5 canonical registry 镜像门禁。

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

fine clean 之后不能把所有 chunk 或人工整理的 JSONL 直接喂给模型：

```text
RAG answer + citations
→ evidence-bound feedback
→ reviewer correction
→ Task / Experience / annotation
→ Experience Compiler
→ train/validation snapshot
```

通过 WebUI/API 提交反馈并由独立 reviewer 确认 expected response、citations、训练用途和许可版本；
随后使用 `bridge_reviewed_feedback.py` 和 `compile_sft_experiences.py`。RTD4 已删除离线 PDF
candidate builder，避免它重新形成无 Experience/annotation 的第二条训练入口。

这一步是受控的 H5 后续阶段，不是 PDF 上传任务的无审批自动步骤：

1. 从已完成的 PDF 问答轨迹中选择训练样本和 validation 样本。
2. 人工审核 annotation，并明确 `training_allowed`、training purpose 和 permission version。
3. 创建并审核 `training_snapshot`。
4. 在 GPU Job 中执行 LoRA，生成 adapter manifest。
5. 用同一固定 evaluation suite 对 base/candidate 评测。
6. 通过 safety scan、独立 reviewer、shadow/canary 和 rollback 检查后，才允许发布 adapter。

不要手工把 `cleaned_corpus`、PDF 原文或临时 QA JSONL 改名为训练集；这会丢失审核证据和来源许可。

完成 WebUI 问答、Memory distillation 和反馈审核后，用同一 root `run_id`
启动 H5 阶段：

```bash
.venv/bin/python scripts/run_pdf_full_cycle.py \
  --stage h5 --run-id <run_id> \
  --suite data/input/pdf-suite.json \
  --environment production
```

生产模式会在 snapshot/release 审批处返回 `waiting_approval`，审批后使用
`--resume`继续。只有隔离的 engineering 模式才允许显式传入 `--allow-auto-approve`；
该结果仅是工程发布预演，不是生产 canary 或 GA。

## 7. 在 WebUI 验证最终问答

完成入库后，在聊天框重复第 2 步的问题。验收以下内容：

- 回答包含 PDF 的实际内容；
- Evidence 显示正确的 `document_id`、`chunk_id`、source version 和页码；
- `/api/sessions/{session_id}/context` 能看到相关 document chunk 和 approved memory；
- 反馈、会话、记忆和任务都属于当前 tenant；
- 如果 adapter 已通过 H5 发布，模型版本/adapter release 能在 H5 页面中查询，否则问答仍使用 base model + RAG。

## 8. 一句话结论

当前项目可以用同一 CLI 和 root `run_id` 分两阶段展示 **PDF → rough clean →
fine clean → PostgreSQL RAG → WebUI 问答 → 会话记忆提炼 → 审核反馈 → GPU LoRA →
评测/发布预演 → adapter reload**。审批、真实 canary 和外部验收仍是独立门禁。
