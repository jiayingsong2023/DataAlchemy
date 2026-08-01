# H1 退出报告：结构化工具结果与独立验证

> 状态：通过工程退出门禁（2026-08-01）。本报告确认 H1 的代码、迁移与隔离数据库轨迹；不代表
> H2 证据 manifest、异步 Job、真实试点或 GA-01 已完成。

## 已交付

- 迁移 `009_harness_verification.sql` 增加 tenant RLS 的 verification attempt 表。部署创建独立
  `dataalchemy_verifier` 登录角色，其只拥有 document/chunk、memory、release 的查询权限。
- strict task 创建时冻结 criterion、verifier 和 ToolSpec digest。`scope_resolver` 在执行前验证参数
  与声明范围相同；required verifier 未通过前不会推进 checkpoint。
- Tool Gateway 将 handler payload 封装为不可变 `ToolResult`，验证 artifact 类型/hash/范围，并只在
  审计事件中保存 digest。失败的 strict 工具同样留下脱敏的失败 ToolResult。
- `verify_ingest`、`verify_retrieval`、`verify_memory`、`verify_release` 使用只读事务；业务失败进入
  `verification_failed`，基础设施问题进入 `verification_blocked`，后者只重试 verifier 且复用原结果。
- `ingest_document` 和 `sync_git` 使用 v2 scope 与 artifact payload；`ingest`、训练、评测和发布入口
  保持明确禁用，等待 H2/H5 的 Job 与证据能力。
- WebUI/API 提供 verification 查询与 CAS 保护的 `POST /api/tasks/{task_id}/retry-verification`；页面显示
  criterion、verifier/version、attempt、结论及 ToolResult digest。

## 验证证据

隔离 PostgreSQL 已应用 009 迁移，并以应用角色和独立 verifier 角色运行：

```bash
TEST_DATABASE_URL='<app-role-url>' \
VERIFIER_DATABASE_URL='<read-only-verifier-role-url>' \
  .venv/bin/pytest -q tests/test_agent_runtime.py tests/test_verifiers.py \
  tests/test_runtime_tools.py tests/test_git_connector.py
```

结果：`24 passed`（仅有既有 `jieba/pkg_resources` 弃用警告）。轨迹覆盖 required verifier 失败不推进、
blocked verifier 只重试验证并复用 ToolResult、结果不可覆盖、scope/artifact 契约、Git 产物、tenant
隔离与只读事务拒绝写入。

同时通过：

```bash
.venv/bin/ruff check --ignore E501,C901 \
  src/core/agent_runtime.py src/core/verifiers.py src/core/runtime_tools.py \
  src/connectors/git.py src/storage/postgres.py tests/test_agent_runtime.py \
  tests/test_verifiers.py tests/test_git_connector.py
.venv/bin/python -m py_compile webui/app.py src/core/agent_runtime.py src/core/verifiers.py
git diff --check
```

`webui/app.py` 保留既有全文件 Ruff 基线问题；H1 新增路由已通过编译与运行时回归，未扩大为无关重构。

## 未关闭项

H2 必须把 ToolResult、verification 与跨存储输入组织成可回放 manifest，并处理 PostgreSQL/MinIO 部分
写入和异步 Job 恢复。H3--H6 与 GA-01 仍按[执行计划](../AGENT_HARNESS_EXECUTION_PLAN.md)推进。
