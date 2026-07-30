# 一份文档的内部试点快速开始

本指南验证最小闭环：把一份 Markdown/TXT 文档上传到 MinIO，管理员在 WebUI 审批入库，
随后在同一 WebUI 中提问并确认答案使用了该文档。

这是一条**内部 Alpha** 路径，不是生产部署说明。生产必须使用 OIDC、非默认对象存储凭据
和 GA-01 试点准入；不要把真实客户数据、个人令牌或默认管理员带入生产环境。

## 完成后你会看到什么

```text
pilot.md → MinIO raw/documents/ → 审批任务 → PostgreSQL 文档/chunks → WebUI 问答
```

首个体验只支持 UTF-8 的 `.md` 或 `.txt`，且不超过 `GIT_MAX_INDEX_BYTES`（默认 1 MiB）。
PDF/DOCX、Jira、Confluence、Git PR 和反馈数据仍走 Spark 批量清洗路径，不应混入本教程。

## 0. 启动内部试点环境

需要 Docker、k3d、kubectl、Helm 3、Python 3.12 与 `uv`。首次使用本仓库的本地集群时：

```bash
export DATABASE_URL='postgresql://dataalchemy_app:password@postgres-host:5432/dataalchemy'
export AUTH_SECRET_KEY='replace-with-a-unique-32-character-minimum-secret'
./scripts/setup/setup_k3d.sh
./scripts/helm-deploy.sh
```

`helm-deploy.sh` 会构建并导入应用与 Operator 镜像，再安装 Helm Chart。它不会替你创建外部
PostgreSQL；上述 `DATABASE_URL` 和 `AUTH_SECRET_KEY` 会写入 Helm Secret。请确保 PostgreSQL
已启用 `pgvector`，并且 MinIO 与 Redis 可访问。

等待 WebUI 与依赖就绪：

```bash
kubectl get pods -n data-alchemy
kubectl get ingress -n data-alchemy
```

为本机访问配置 `data-alchemy.test` 指向 k3d load balancer 后，打开 `http://data-alchemy.test`。
如果使用受管服务或已有 Kubernetes 集群，请使用相同的 Helm Chart、Secret 和 Ingress；不要
运行旧的 `scripts/pilot_up.sh`，它不是当前试点的一键入口。

## 1. 创建一份可验证的示例文档

在仓库根目录创建 `pilot.md`：

```markdown
# Aurora 支持窗口

Aurora 团队每周二和周四 09:00–17:00（Asia/Shanghai）提供支持。
紧急 P1 事件请在工单中标记 `severity: P1`。
```

上传到规定的原始文档前缀。脚本只负责安全地落入 MinIO，**不会**自动建索引：

```bash
uv run python scripts/ops/manage_minio.py upload pilot.md
uv run python scripts/ops/manage_minio.py list
```

输出中应出现：`raw/documents/pilot.md`。若 MinIO 不在默认地址，请先设置
`S3_ENDPOINT`、`AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY` 与 `S3_BUCKET`。

## 2. 在 WebUI 审批并导入文档

1. 以内部试点管理员登录 WebUI。仅开发环境允许本地账户；生产环境必须走 OIDC。
2. 在左侧 **Agent Tasks** 区点击“导入文档”图标。
3. 输入 `raw/documents/pilot.md`。系统创建 `ingest_document` 任务，状态应为
   `waiting_approval`。
4. 点击任务，在详情中选择 **Approve**。工具会读取 MinIO 原始对象，检查路径、大小、编码、
   二进制内容和疑似密钥，按 Markdown/TXT 策略分块后原子写入 PostgreSQL。
5. 任务状态变为 `succeeded` 后，管理员审计面板应出现 `document.ingest` 事件。

如果任务失败，请不要手工向 PostgreSQL 写数据。修复原始文档或对象存储连接后，在任务详情中
选择 **Retry**。

## 3. 在同一 WebUI 验证问答

在聊天框输入：

```text
Aurora 团队的支持时间是什么？P1 事件如何标记？
```

预期答案应包含“周二和周四 09:00–17:00（Asia/Shanghai）”以及 `severity: P1`。若答案未
命中，先确认入库任务成功，再检查本地 embedding/reranker 模型与 PostgreSQL 连接；不要通过
把文档复制到提示词来伪造检索成功。

## 4. 验证与清理

管理员可在任务详情、**Audit** 面板和 PostgreSQL 中核对入库操作。保留原始对象以便重放；
若要撤销该试点文档，应使用受控删除流程，而不是直接删除数据库行。

```bash
TEST_DATABASE_URL='<isolated-test-database-url>' \
  uv run pytest -q tests/test_runtime_tools.py tests/test_git_connector.py
```

本指南的边界是“一份已授权的文本文件”。多源批处理、Spark、训练、云增强和真实团队试点的
要求见 [当前软件架构](./ARCHITECTURE.md)、[发布状态](./RELEASE_STATUS.md) 与
[GA-01 试点包](./release/GA01_PILOT_PACK.md)。
