# H0 设计：任务契约、计划版本与统一运行编号

> 分支：`feat/harness-h0-task-contract`。本设计落实
> [H0 工作包](../AGENT_HARNESS_EXECUTION_PLAN.md)，但不实现 verifier、异步 Job、证据包、
> 自动记忆或 LoRA 门禁；它们分别属于 H1--H5。

## 1. 目标与边界

H0 让一个复杂任务在执行前具有可查询、可审计的完成定义，并能在不改写已执行步骤的前提下
修订剩余计划。

```text
TaskSpec + plan_json + run_id
        → 执行当前步骤
        → 观察结果
        → 暂停后修订剩余计划
```

H0 的“成功谓词”是任务契约中的声明，尚不作为自动放行依据；H1 的独立 verifier 才会将其
变成可执行证据。一个工具返回 `completed` 仍不能关闭复杂任务。

## 2. 最小数据模型

不新建 `task_steps`、`runs` 或第二套状态机。`agent_tasks` 已拥有 tenant RLS、计划、审批、
检查点和恢复语义，H0 仅新增三列：

| 列 | 类型 | 用途 |
| --- | --- | --- |
| `run_id` | UUID，唯一、非空 | 一次任务执行的稳定关联键；H0 中一个 task 对应一个 run。H2 再用它关联 Job、manifest 和发布记录。 |
| `task_spec_json` | JSONB，非空 | 创建时冻结的契约；不随 replan 修改。 |
| `plan_version` | INTEGER，非空，默认 1 | 只在计划修订时递增；不复用现有 `version`，后者是任意行状态更新的乐观并发版本。 |

迁移为历史任务回填 `run_id = task_id`，并写入 `{"schema_version": 0, "legacy": true}`。
这使部署升级不破坏已有任务或 RLS。新任务使用 UUID `run_id`，不得由客户端提供。

`task_spec_json` 的 v1 结构如下；tenant、owner、role、实际允许工具和审批要求均由服务端
从身份、注册表和计划推导，客户端不能伪造：

```json
{
  "schema_version": 1,
  "success_criteria": ["检索结果必须包含已授权来源"],
  "data_scope": {"source_refs": ["connector:git:repo-a"]},
  "limits": {"max_steps": 6, "deadline_seconds": 900},
  "created_by": "alice",
  "created_at": "2026-08-01T00:00:00Z"
}
```

`goal`、`plan_json`、`budget_json`、tenant、owner 和 role 继续使用现有列作为运行时权威，避免
同一字段出现两个可变来源。`success_criteria` 和 `data_scope` 只描述业务契约；H0 不解释任意
自然语言谓词，也不把它们送给工具执行。

## 3. API 与计划语义

### 创建

扩展 `POST /api/tasks`，保持当前单工具请求兼容：

```json
{
  "goal": "从已授权来源生成支持窗口报告",
  "steps": [
    {"tool": "sync_git", "arguments": {}},
    {"tool": "rag_chat", "arguments": {"query": "Aurora 支持窗口"}}
  ],
  "success_criteria": ["回答引用已同步且已授权的文档"],
  "data_scope": {"source_refs": ["connector:git:repo-a"]},
  "deadline_seconds": 900
}
```

- 未提供 `steps` 时，服务端将旧的 `tool` + `arguments` 规范化为一个步骤。
- 服务端在写入前检查步骤数量、工具存在性、调用者角色和参数 schema；运行时仍在每一步重新
  检查角色与限流，避免权限在长任务期间变化后继续生效。
- 对 `ToolSpec.idempotent=True` 的步骤，若调用方未给 key，服务端生成
  `<task_id>:<plan_version>:<step_index>`。同一 task 的 retry 复用该 key；新的 task 有新的 key。
- `max_steps` 由服务端从 `limits.max_steps` 或 API 上限得出，不能小于 plan 长度。
- 创建事件包含 `run_id`、`plan_version`、TaskSpec schema version 和脱敏后的计划。敏感参数继续
  使用现有 `ToolSpec.sensitive_fields` 脱敏。

### 暂停后修订（H0 的 replan）

H0 不让 LLM 在运行中任意改图，也不新增图编排器。新增受认证的
`POST /api/tasks/{task_id}/replan`：

1. 仅 task owner 或 admin 可在 `paused`、`waiting_approval` 或 `failed` 状态提交。
2. 已执行的 `current_step` 前缀不可修改；请求只携带替换后的剩余步骤及必填 `reason`。
3. 服务端重做创建时的 schema/角色/幂等键检查，递增 `plan_version`，清除旧步骤的待审批信息。
4. 写入 `replanned` 事件：旧/新版本、原因、操作者、已执行步数、旧/新计划 hash 与脱敏后的
   剩余计划。原始 TaskSpec 不变。
5. 任务恢复后从当前未执行步骤继续。若新首步需审批，现有审批机制再次生效。

未来的 LLM planner 只能调用同一 replan API，因此也会留下相同证据；H0 不假设或实现该 planner。

## 4. 运行时改动

| 位置 | H0 改动 |
| --- | --- |
| `src/storage/migrations/008_harness_task_contract.sql` | 新增列、历史回填、约束和 `run_id` 索引；复用现有 `agent_tasks` RLS policy。 |
| `src/core/agent_runtime.py` | TaskSpec 验证/冻结、run_id 生成、步骤规范化、自动幂等键、`plan_version` 和 `replan()`。 |
| `webui/app.py` | 扩展任务请求模型；增加受身份保护的 replan endpoint；旧请求保持工作。 |
| `webui/static/*` | 只显示 run ID、计划版本、TaskSpec 摘要和暂停后 replan 操作；完整时间线留给 H3。 |
| `tests/test_agent_runtime.py` | 多步骤、自动幂等、计划冻结、权限、replan、恢复及 tenant 隔离覆盖。 |

H0 不修改 Spark、Connector、训练和 Release 代码；它们在 H1/H2 获得结构化结果和 run 关联后再接入。

## 5. 状态与失败规则

- 新任务：`created → running → waiting_approval/running → succeeded|failed|cancelled`。
- replan 不新增状态；只能在现有安全停点修改尚未执行的后缀。
- 创建或 replan 校验失败时不写 task/新版本；运行时工具失败仍走既有 `failed` 语义。
- retry 不递增 `plan_version`，也不产生新 run；它从相同 checkpoint 与 idempotency key 重试。
- H0 不允许以 replan 绕过审批、扩大工具权限或修改 tenant/data scope。扩大范围需要新 task。

## 6. 测试与退出门禁

实现完成前必须新增并通过：

```bash
TEST_DATABASE_URL='<isolated-test-database-url>' \
  .venv/bin/pytest -q tests/test_agent_runtime.py tests/test_runtime_tools.py
.venv/bin/ruff check src/core/agent_runtime.py webui/app.py tests/test_agent_runtime.py
git diff --check
```

必测案例：

1. 两步任务在第一步审批后继续到第二步，事件和查询均带同一 `run_id`。
2. 服务端为幂等副作用步骤生成 key，retry 不重复调用；不同 task 不共享 key。
3. 暂停后只可替换未执行后缀，`replanned` 事件保留版本、理由与计划 hash。
4. 普通用户不能 replan 他人任务、扩大 scope 或越权添加管理员工具；跨 tenant 查询为零。
5. 不合法 TaskSpec、超步数、缺少成功谓词、非法 deadline 或参数 schema 在执行前失败且无副作用。

H0 退出条件是：一个多步骤任务可在审批、失败与暂停后被安全恢复或修订，且审计能够解释
“为何执行此计划、执行到哪一步、何时由谁改动”。它还不声称任务业务目标已被独立验证。
