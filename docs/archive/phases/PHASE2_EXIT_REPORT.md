# Phase 2 退出报告

日期：2026-07-30
分支：`feat/phase-2-memory-governance`

## 结论

Phase 2 的本地推出门禁通过。PostgreSQL + pgvector 是文档、混合检索、任务事件和治理记忆的唯一权威；Redis 仅承担带租户作用域的 TTL 缓存、会话、锁和队列，MinIO 仅保留原始不可变对象。

## 门禁证据

| 门禁 | 结果 | 可复现命令/证据 |
| --- | --- | --- |
| 单一检索权威 | 通过 | `pyproject.toml`、`uv.lock`、运行时代码已无 FAISS/rank-bm25/RAG SQLite/S3 索引同步；`uv lock --locked` 通过并保留 ROCm wheel 源。 |
| 数据库迁移与 pgvector | 通过 | 在干净 `phase2_clean` 数据库执行 `DATABASE_URL=... .venv/bin/python scripts/migrate_postgres.py`，应用 001--004 迁移。 |
| 非所有者应用角色与 RLS | 通过 | `dataalchemy_app` 是 `NOSUPERUSER`；以该角色执行运行时与记忆测试，跨租户任务、记忆和文档检索均为零。 |
| 文档检索与删除 | 通过 | `tests/test_memory_orchestrator.py` 覆盖 pgvector 文档召回、跨租户零召回和删除后零召回。 |
| 记忆治理 | 通过 | `scripts/evaluate_phase2_memory.py`：20/20 审批后 Recall@1，未审批和跨租户召回均为 0。更正创建候选替代项，原项标记 superseded。 |
| Agent Runtime | 通过 | `tests/test_agent_runtime.py`：PostgreSQL 事件、审批、重试、幂等性和租户边界通过。 |
| Phase 1 基线 | 通过 | `scripts/evaluate_phase1_baseline.py`：5/5 控制面任务通过；`eval/phase1_real_task_results.json` 保留 5/5 真实 RAG 基线。 |
| 备份恢复 | 通过 | `pg_dump -Fc` 后恢复至 `phase2_restore`；已验证 vector 扩展和 7 条任务记录。 |
| 部署与 CI | 通过（本地预检） | `helm lint deploy/charts/data-alchemy` 与启用 PostgreSQL 的 `helm template` 通过；CI 已配置 pgvector 服务、应用角色、迁移、测试和评测。 |

## 最终复验

```bash
.venv/bin/pytest -q
TEST_DATABASE_URL=postgresql://... .venv/bin/pytest -q \
  tests/test_agent_runtime.py tests/test_memory_orchestrator.py \
  tests/test_runtime_tools.py tests/test_reranker_device.py
DATABASE_URL=postgresql://... .venv/bin/python scripts/evaluate_phase2_memory.py
DATABASE_URL=postgresql://... .venv/bin/python scripts/evaluate_phase1_baseline.py
uv lock --locked
helm lint deploy/charts/data-alchemy
```

备注：首次上线前仍须在受管 PostgreSQL/MinIO/Redis 环境执行同一组命令，并由 CI 的托管运行结果替代本地预检记录。
