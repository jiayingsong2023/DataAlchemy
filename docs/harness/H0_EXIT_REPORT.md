# H0 退出报告：任务契约与安全执行基线

> 状态：通过工程退出门禁（2026-08-01）。本报告仅确认代码、迁移和隔离数据库测试；不代表
> H1 verifier、真实试点或 GA-01 已完成。

## 已交付

- 迁移 `008_harness_task_contract.sql` 为任务增加 `run_id`、冻结 TaskSpec、计划版本、lease/
  heartbeat、暂停/取消请求；为工具调用增加 task/step/run/attempt 生命周期字段。
- `AgentRuntime` 支持 strict 多步骤任务、服务器生成的 `step_id` 与
  `<run_id>:<step_id>` 幂等键、deadline、lease/CAS、协作式暂停/取消、replan 和安全接管。
- strict 计划执行完毕只进入 `awaiting_verification`；legacy 仅兼容单步骤无副作用工具。
  不确定的副作用结果进入 `reconciliation_required`，不会自动重放。
- WebUI/API 可提交 strict TaskSpec，携带并发 `expected_version` 控制任务，并显示 run、计划版本、
  执行模式和真实状态。现有导入文档入口已转为 strict。
- PostgreSQL 事务层只包装数据库驱动异常，避免控制冲突或契约拒绝被错误地转换为持久化故障。

## 验证证据

隔离 PostgreSQL 库已应用迁移 `008_harness_task_contract.sql`，随后以应用角色执行：

```bash
TEST_DATABASE_URL='<isolated-test-database-url>' \\
  .venv/bin/pytest -q tests/test_agent_runtime.py \\
  tests/test_memory_governance.py tests/test_memory_orchestrator.py
```

结果：`16 passed`（仅有既有 `jieba/pkg_resources` 弃用警告）。测试覆盖 legacy 兼容、strict
契约、审批、幂等重试、副作用 reconciliation、原子 replan、暂停/取消、并发 lease、tenant
隔离及既有记忆调用回归。

同时通过：

```bash
.venv/bin/ruff check --ignore E501,C901 \\
  src/core/agent_runtime.py src/core/runtime_tools.py src/storage/postgres.py \\
  tests/test_agent_runtime.py
.venv/bin/python -m py_compile webui/app.py
git diff --check
```

`webui/app.py` 存在 H0 之前的全文件 Ruff 基线问题（导入顺序、FastAPI `Depends` 与既有复杂度）；
本次未扩大到无关重构。新增 API 代码已通过编译和定向运行时测试。

## 未关闭项

H1 必须提供独立 verifier、结构化 `ToolResult`、真实 scope resolver 和证据策略，才能把 strict
任务从 `awaiting_verification` 推进为 `succeeded`。H2--H6 与 GA-01 按
[执行计划](../AGENT_HARNESS_EXECUTION_PLAN.md) 继续。
