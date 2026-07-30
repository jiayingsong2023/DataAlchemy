# Phase 3 Git 试点快速开始

## 最小前提

- PostgreSQL（含 pgvector）、Redis、MinIO 已由 Helm 或受管服务提供；
- 一个 GitHub 仓库与最小只读令牌；令牌仅授予试点仓库读取权限；
- 已设置 `DATABASE_URL`、`REDIS_URL`、`S3_ENDPOINT`、
  `GIT_PILOT_REPOSITORY` 和 `GIT_PILOT_TOKEN`。

## 启动与自检

```bash
./scripts/pilot_up.sh
```

脚本部署 Helm chart、执行迁移并验证 pgvector 和配置。诊断只显示状态，不显示令牌或
连接字符串。

## 首项任务

1. 以管理员身份创建 `sync_git` 任务，并提供唯一 `idempotency_key`。
2. 在控制台批准该任务；成功后会生成 `PILOT_RUNS_DIR/runs/{run_id}/manifest.json`。
3. 用普通试点用户执行 `rag_chat`，确认回答只引用其有权访问的 Git 内容。

## 恢复与撤销

- 临时网络失败不会推进连接器游标，批准重试即可恢复。
- 撤销源对象或权限后，重新同步会删除对应文档块并使检索归零。
- 不要提交令牌、原始仓库内容或试点评测答案。
