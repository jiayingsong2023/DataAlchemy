# 本地 GPU 集群：从 PDF 到问答的操作手册

本文用于在本机销毁并重建 `dataalchemy-gpu`，然后验证：

```text
PDF → MinIO raw → Spark rough clean → deterministic fine clean/refine
    → PostgreSQL documents/chunks → RAG → session memory
    →（审核后的训练候选）LoRA GPU Job → evaluation/release → WebUI 问答
```

这是**本地测试流程**。删除集群会删除该集群内的 Pod、Job、PVC 和 containerd
镜像缓存，但不会自动删除主机上的 `data/` 目录。执行前确认当前集群不是生产环境。

## 0. 前置条件

- Docker、`k3d`、`kubectl`、Helm 3、Python 3.12 和项目 `.venv` 已安装；
- AMD ROCm/AMD CDI 已配置，`amd-ctk cdi validate` 通过；
- 已准备 Web、H5 模型 Job 和 Spark ETL 三个本地镜像。不得再用同一个完整镜像兼任三种职责；
  所有本地 cache-backed 标签都不代表 H5 canonical 发布门禁已关闭；
- 本地已有以下基础镜像，或者允许 Docker 拉取：
  `pgvector/pgvector:pg16`、MinIO、`redis:7.0-alpine`；
- PDF 是可复制文本、未加密、未损坏且不超过 25 MiB。扫描件 OCR、复杂表格和密码保护
  PDF 当前不在支持范围内。

设置变量（只影响本次终端）：

```bash
export CLUSTER_NAME=dataalchemy-gpu
export BUILD_GIT_SHA="$(git rev-parse HEAD)"
export WEB_IMAGE="data-alchemy:web-${BUILD_GIT_SHA}"
export HARNESS_IMAGE="data-alchemy:h5-${BUILD_GIT_SHA}"
export ETL_IMAGE="data-alchemy:etl-${BUILD_GIT_SHA}"
export OPERATOR_IMAGE=dataalchemy-operator:h5-local
export MINIO_RELEASE=minio/minio:RELEASE.2025-04-22T22-12-26Z
export MINIO_IMAGE=minio/minio:latest
export REDIS_IMAGE=redis:7.0-alpine
export PG_IMAGE=pgvector/pgvector:pg16
```

## 1. 删除旧集群

先确认只删除目标集群，再执行删除：

```bash
test "$CLUSTER_NAME" = dataalchemy-gpu
k3d cluster list
k3d cluster delete "$CLUSTER_NAME" || true
```

如果需要清空主机挂载的数据，先备份 `data/`，再只删除明确的测试目录；不要对项目根目录
执行递归删除。集群删除本身不会删除主机 `data/`。

## 2. 准备并导入镜像

代码或依赖变更后重新构建；每个目标只安装自己的依赖组：

```bash
docker build --target webui --build-arg BUILD_GIT_SHA="$BUILD_GIT_SHA" -t "$WEB_IMAGE" .
docker build --target harness-job --build-arg BUILD_GIT_SHA="$BUILD_GIT_SHA" -t "$HARNESS_IMAGE" .
docker build -f Dockerfile.harness -t "$ETL_IMAGE" .
docker image inspect "$WEB_IMAGE" "$HARNESS_IMAGE" "$ETL_IMAGE" >/dev/null
docker build -t "$OPERATOR_IMAGE" deploy/operator/
```

如果 MinIO 只有版本化标签，给 Operator 使用的 `latest` 标签建立本地别名：

```bash
docker image inspect "$MINIO_RELEASE" >/dev/null
docker tag "$MINIO_RELEASE" "$MINIO_IMAGE"
docker image inspect "$REDIS_IMAGE" >/dev/null || docker pull "$REDIS_IMAGE"
docker image inspect "$PG_IMAGE" >/dev/null
```

## 3. 创建干净 GPU 集群

`setup_k3d.sh` 默认创建 `dataalchemy-gpu`，并启用 `--gpus all`：

```bash
K3D_CLUSTER_NAME="$CLUSTER_NAME" K3D_GPU_ENABLED=true \
  ./scripts/setup/setup_k3d.sh
k3d kubeconfig merge "$CLUSTER_NAME" --switch-context
kubectl get nodes -o wide
kubectl wait --for=condition=Ready node --all --timeout=5m
```

GPU 模式创建前会检查宿主机 AMD runtime 与 `/etc/cdi/amd.json`，并将 CDI 注册表挂载进
每个 K3d 节点。部署时必须显式启用 WebUI GPU 安全上下文；部署脚本会在 Helm 完成后执行
真实 PyTorch 门禁，`torch.cuda.is_available()` 或设备数为假即失败：

```bash
sudo amd-ctk cdi generate --output=/etc/cdi/amd.json
sudo amd-ctk cdi validate
K3D_GPU_ENABLED=true ./scripts/setup/setup_k3d.sh
helm upgrade --install data-alchemy deploy/charts/data-alchemy \
  --namespace data-alchemy --create-namespace --wait --timeout 15m \
  --set webui.gpu.enabled=true
bash scripts/setup/verify_gpu.sh data-alchemy
```

导入所有离线镜像。未导入的镜像会在 `imagePullPolicy: Never` 下直接失败：

```bash
k3d image import "$WEB_IMAGE" "$HARNESS_IMAGE" "$ETL_IMAGE" "$OPERATOR_IMAGE" "$MINIO_IMAGE" \
  "$REDIS_IMAGE" "$PG_IMAGE" -c "$CLUSTER_NAME"
```

## 4. 部署完整 DataAlchemy 栈

本地测试使用随机密码；生产环境应改用 Secret 管理。`postgresql.enabled=true` 会在集群
内创建 pgvector，Operator 会创建 Redis 和 MinIO。

```bash
export AUTH_SECRET_KEY="$(openssl rand -hex 32)"
export PG_PASSWORD="$(openssl rand -hex 24)"
export PG_APP_PASSWORD="$(openssl rand -hex 24)"
export PG_VERIFIER_PASSWORD="$(openssl rand -hex 24)"

helm upgrade --install data-alchemy deploy/charts/data-alchemy \
  --namespace data-alchemy --create-namespace \
  --wait --timeout 15m \
  --set images.core="$WEB_IMAGE" \
  --set images.harnessJob="$HARNESS_IMAGE" \
  --set images.etl="$ETL_IMAGE" \
  --set images.operator="$OPERATOR_IMAGE" \
  --set images.pullPolicy=Never \
  --set config.harnessJobGpuEnabled=true \
  --set config.harnessJobGpuPrivileged=true \
  --set-string webui.gpu.rocmHostPath=/opt/rocm \
  --set webui.gpu.enabled=true \
  --set postgresql.enabled=true \
  --set-string credentials.authSecretKey="$AUTH_SECRET_KEY" \
  --set-string credentials.postgresPassword="$PG_PASSWORD" \
  --set-string credentials.postgresAppPassword="$PG_APP_PASSWORD" \
  --set-string credentials.postgresVerifierPassword="$PG_VERIFIER_PASSWORD"
```

等待 Operator 创建的组件：

```bash
kubectl -n data-alchemy rollout status deployment/dataalchemy-operator --timeout=5m
kubectl -n data-alchemy get pods,svc,pvc
kubectl -n data-alchemy wait --for=condition=available deployment --all --timeout=15m
kubectl -n data-alchemy get pods
```

本地访问 WebUI 时，将 Ingress 主机名解析到 k3d 端口：

```bash
grep -qF 'data-alchemy.test' /etc/hosts || \
  echo '127.0.0.1 data-alchemy.test' | sudo tee -a /etc/hosts
curl -fsS -H 'Host: data-alchemy.test' http://127.0.0.1/metrics
```

## 5. 迁移数据库并做基础检查

PostgreSQL 是 ClusterIP，主机脚本通过临时端口转发执行迁移：

```bash
kubectl -n data-alchemy port-forward svc/postgresql 15432:5432 >/tmp/dataalchemy-pg-forward.log 2>&1 &
PG_FORWARD_PID=$!
trap 'kill "$PG_FORWARD_PID" 2>/dev/null || true' EXIT

export DATABASE_URL="postgresql://dataalchemy_app:${PG_APP_PASSWORD}@127.0.0.1:15432/dataalchemy"
.venv/bin/python scripts/migrate_postgres.py
.venv/bin/python scripts/pilot_check.py
```

同时确认运行时依赖：

```bash
kubectl -n data-alchemy get deploy dataalchemy-redis dataalchemy-minio webui coordinator dataalchemy-operator
kubectl -n data-alchemy get jobs,pods
```

## 6. 输入 PDF 并验证 rough clean、fine clean、RAG

1. 将 PDF 放在本机明确的测试目录，例如 `data/input/pilot.pdf`；不要直接改写
   `data/raw`，raw 对象应由受控入口写入 MinIO。
2. 打开 `http://data-alchemy.test`，登录本地测试身份。
3. 在 **Agent Tasks** 中上传 `pilot.pdf`，创建并启动导入任务。
4. 在同一个 `run_id` 中等待并核对以下阶段：

   | 阶段 | 应看到的证据 |
   | --- | --- |
   | Input validation | PDF SHA-256、source version、ACL snapshot |
   | Spark rough clean | Job 成功、accepted/rejected/quarantined 数量、rough artifact hash |
   | Fine clean/refine | `canonical_content` spans、`rag_projection` chunks、locator、PII/injection 检查 |
   | Publish | PostgreSQL `documents`、`document_chunks`、ACL、向量/FTS |
   | RAG probe | 命中的 chunk、source version、PDF 页码引用 |

5. 用 PDF 中确定存在的事实提问，确认回答带有正确页码或 chunk 引用。若 Spark Job 失败，
   保留 raw 和失败 evidence，从首个未验证阶段恢复，不要手工把 raw 文件标记为已清洗。

## 7. 将问答会话提炼到 Memory

PDF 原文不会直接成为长期记忆；先通过 RAG 问答，再对会话做 distillation：

```bash
curl -X POST "http://data-alchemy.test/api/sessions/$SESSION_ID/distill" \
  -H "Authorization: Bearer $TOKEN"
```

在 WebUI Memory 面板或 `/api/memories` 检查状态。个人低风险记忆可自动批准，团队/租户
记忆仍遵循审核与 ACL；冲突或拒绝项不得进入后续上下文。

## 8. 使用 PDF 数据触发 LoRA（受控 H5 流程）

上传 PDF **不会自动触发 LoRA**。必须从已验证的 PDF 问答轨迹生成有监督样本，并明确人工
审核、训练许可和固定评测集：

```text
canonical spans + evidence-bound reviewed QA
  → reviewed feedback bridge
  → Experience/annotation（人工批准）
  → Experience Compiler
  → candidate training snapshot（独立批准）
  → base evaluation
  → GPU LoRA Job
  → adapter evaluation / safety scan
  → shadow/canary / promote 或 rollback
```

将已批准 feedback 投影到受治理 Experience：

```bash
.venv/bin/python scripts/bridge_reviewed_feedback.py --help
.venv/bin/python scripts/compile_sft_experiences.py --help
```

annotation 必须包含 `review_status=approved`、`training_allowed=true`、`split_group`、source spans
和权限版本，再由唯一 Experience Compiler 创建 snapshot。旧离线 PDF candidate builder 已删除；
`run_pdf_full_cycle.py --stage h5` 直接 snapshot 路径已 fail-closed，不再是生产入口。

### 8.1 人工审核入口与操作

当前 WebUI 只显示 H5 annotation/snapshot 状态和数量，**没有人工审核表单**。reviewer/admin
应使用 FastAPI Swagger（`http://data-alchemy.test/docs`）或以下 REST API；普通用户无审核权限，
且任务创建者不能审核自己的 annotation：

```bash
export BASE_URL=http://data-alchemy.test
export TOKEN='<reviewer-jwt>'

curl -fsS -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/api/h5/annotations"
```

审核每条 annotation 时至少核对 task、environment snapshot、独立 verifier、原始输出、
`expected_response`、split、来源 hash/ACL 和训练用途。无效任务或不可复现环境应拒绝并重新
rollout；holdout、目标模型已解决的样本以及没有正确答案的 bad feedback 不得授权训练。

批准可用于训练的样本（批准时即使 `training_allowed=false` 也必须填写 purpose/version）：

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$BASE_URL/api/h5/annotations/<annotation_id>/decision" \
  -d '{"status":"approved","training_allowed":true,"training_purpose":"pdf-gap-sft","permission_version":"pilot-v1","reason":"evidence and expected answer verified"}'
```

拒绝或撤销：

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$BASE_URL/api/h5/annotations/<annotation_id>/decision" \
  -d '{"status":"rejected","reason":"invalid expected answer"}'
```

审核 WebUI 的 good/bad feedback 使用独立入口；`feedback_id` 来自聊天反馈响应：

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$BASE_URL/api/feedback/review" \
  -d '{"feedback_id":"<feedback_id>","review_status":"approved","training_allowed":true,"training_purpose":"reviewed-feedback-sft","permission_version":"pilot-v1","reason":"answer and evidence verified"}'
```

对双模型 re-rollout 的 weak/failed 样本，DeepSeek 产生的 `llm_judge` annotation 必须继续保留
`human_reviewed=false`，不能通过重新批准伪装成人工标签。当前版本尚无 WebUI 或公开 REST
接口创建新的 `human_review` annotation；生产人工校准仍需通过内部
`EvaluationService.create_annotation` 另建不可变标签，至少记录 task/run/trial/split、
experience ref/hash、`expected_response`、rubric、原 judge annotation ID、审核理由及
`human_reviewed=true`，再由另一名 reviewer 执行上述 decision。这个缺口关闭前，自动审核数据
只能用于 synthetic 工程验证，不能关闭生产门禁。

annotation 获批只表示“允许被编译”，不会自动训练或发布。下一步显式运行：

```bash
.venv/bin/python scripts/compile_sft_experiences.py --help
```

编译器只消费显式传入且通过独立 verifier 的 annotation/experience，并生成 candidate snapshot。
snapshot 创建者之外的 reviewer 还必须单独批准：

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$BASE_URL/api/h5/training-snapshots/<snapshot_id>/decision" \
  -d '{"decision":"approve"}'
```

完整状态流为：`annotation approved → explicit compiler → candidate snapshot → snapshot approved
→ GPU training → A/B evaluation → release gate`。任何人工批准都不会跳过后续 verifier、评测或
发布门禁。

## 9. 最终 WebUI 验收

再次提问同一事实，记录：

- 回答引用了 PDF 的正确 document/chunk/page；
- RAG 使用当前 tenant 的 PostgreSQL 数据；
- Memory 面板显示本会话已批准的提炼结果；
- 若 LoRA 已通过 H5 发布，显示确切 adapter/release 版本；否则明确显示 base model + RAG；
- WebUI、任务、反馈、记忆和训练候选均属于当前 tenant。

## 10. 故障定位与再次重置

```bash
kubectl get pods -A
kubectl -n data-alchemy describe pod <pod>
kubectl -n data-alchemy logs deploy/dataalchemy-operator
kubectl -n data-alchemy logs deploy/webui
```

最常见的本地问题是 `ErrImageNeverPull`：重新检查 `docker image list`，然后再次执行
`k3d image import ... -c dataalchemy-gpu`。需要从头开始时，回到第 1 步；不要删除共享或生产
数据库，也不要把 synthetic H5 rehearsal 当作真实 PDF LoRA 验收。

相关说明：[PDF 端到端快速开始](./PDF_END_TO_END_QUICKSTART.md)、[H5 设计](./harness/H5_EVALUATION_RELEASE_DESIGN.md)。

## 11. WebUI 导入入口与显式训练闭环

`scripts/run_pdf_full_cycle.py --stage webui` 保留工程环境的 PDF 导入与 RAG 验证。旧 H5
阶段已关闭，避免绕过 Experience Compiler 直接创建 snapshot。

工程环境从 PDF 到 WebUI：

```bash
.venv/bin/python scripts/run_pdf_full_cycle.py \
  --stage webui \
  --pdf data/input/pilot.pdf \
  --reset --confirm-cluster-reset dataalchemy-gpu --deploy \
  --environment engineering --allow-auto-approve
```

脚本输出 JSON receipt，并在 `data/runs/<run_id>/receipt.json` 保存不含密钥的本地副本。
用户随后在 WebUI 中提问、关闭会话生成 Memory、提交反馈并完成审核。训练数据必须显式执行：

```bash
.venv/bin/python scripts/compile_sft_experiences.py --help
```

compiler 只接受带 evidence、独立 verifier 和有效许可的 approved annotation；snapshot 还需
独立 reviewer 批准。生产环境不允许自动批准，审批处会返回 `waiting_approval`；真实 canary
必须提供实测 observation，不能用本地合成指标宣称发布通过。
`data/input/pdf-suite.json` 必须由试点负责人提供固定 cases 和 `required_substrings`，不能使用空断言。
最小格式如下：

```json
{"version":"pdf-v1","policy_version":"pdf-v1","cases":[
  {"case_id":"c1","query":"文档的核心结论是什么？","input_sha256":"<64位SHA256>","required_substrings":["<人工确认答案片段>"]}
]}
```
