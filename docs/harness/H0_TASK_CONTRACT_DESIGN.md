# H0 设计：可验证任务契约与安全执行基线

> 分支：`feat/harness-h0-task-contract`。本设计落实
> [Agent Harness 执行计划 H0](../AGENT_HARNESS_EXECUTION_PLAN.md)。H0 建立任务身份、计划、
> 幂等、并发和控制语义；独立 verifier、异步 Job 与证据包分别在 H1/H2 实现。

## 1. 目标、完成定义与非目标

H0 要回答五个问题：为何执行、允许执行什么、由谁执行、执行到哪里、计划为何改变。

```text
TaskSpec + run_id + stable step_id
        → lease owner 执行
        → attempt / checkpoint
        → pause、cancel、retry 或原子 replan
        → legacy succeeded / strict awaiting_verification
```

H0 不把工具返回值当成业务成功。`strict` 任务执行完计划后进入 `awaiting_verification`；H1 的独立
verifier 通过后才能进入 `succeeded`。H0 期间只有兼容的无副作用 `legacy` 任务可直接成功。

H0 不实现：

- verifier 执行与证据包；
- Spark/训练 Kubernetes Job 接入；
- LLM 自动 planner、memory distillation 或 LoRA 发布；
- 强制终止 Python 线程。运行中取消采用协作式安全停点，异步 Job 的终止留给 H2。

## 2. 核心不变量

1. 一个 task 对应一个稳定 `run_id`；retry/replan 不产生新 run。
2. 一个逻辑步骤对应一个稳定 `step_id`；retry 和保留步骤不更换 ID。
3. 同一 task 同一时刻只有一个有效 lease owner 能推进状态。
4. 已执行计划前缀不可修改；replan 只能替换未执行后缀。
5. TaskSpec 创建后不可修改；扩大工具或数据范围必须创建新 task。
6. 带副作用步骤的结果不确定时不得盲目重试，必须进入 reconciliation/manual 状态。
7. 所有状态、计划和工具运行写入都使用 tenant RLS 与现有 `version` compare-and-swap。

## 3. 数据模型

H0 复用 `agent_tasks`、`agent_events` 和 `agent_tool_runs`，不新增第二套运行表或 `task_steps` 表。

### 3.1 `agent_tasks`

迁移 `008_harness_task_contract.sql` 新增：

| 列 | 类型 | 规则 |
| --- | --- | --- |
| `run_id` | UUID | 唯一、非空；历史任务回填 `task_id`。 |
| `task_spec_json` | JSONB | 非空、创建后冻结；历史任务标记 `schema_version: 0`。 |
| `plan_version` | INTEGER | 非空、默认 1；只在 replan 时递增。 |
| `lease_owner` | TEXT | 当前执行者；不保存凭据。 |
| `lease_expires_at` | TIMESTAMPTZ | 租约到期后才允许安全接管。 |
| `heartbeat_at` | TIMESTAMPTZ | 长步骤执行期间刷新。 |
| `pause_requested` | BOOLEAN | 默认 false；在下一个安全停点暂停。 |
| `cancel_requested` | BOOLEAN | 默认 false；在下一个安全停点取消。 |

现有 `version` 继续作为所有状态写入的并发版本，不能替代 `plan_version`。现有 `current_step`
仍是已完成步骤数，也是 checkpoint 位置。

新增状态：

- `pausing` / `cancelling`：工具仍在运行，不能谎报已暂停或取消；
- `awaiting_verification`：strict 计划已执行，但尚未由 H1 verifier 证明业务完成；
- `reconciliation_required`：副作用可能已经发生但结果不确定，禁止自动重放。

### 3.2 `plan_json`

计划仍保存在现有 JSONB 列，每个步骤规范化为：

```json
{
  "step_id": "0f1e...",
  "tool": "sync_git",
  "arguments": {},
  "scope_refs": ["connector:git:repo-a"],
  "idempotency_key": "<run_id>:<step_id>",
  "created_in_plan_version": 1
}
```

- `step_id` 和默认 idempotency key 由服务端生成，客户端不能覆盖。
- replan 保留的步骤连同 `step_id` 原样保留；替换步骤获得新 ID。
- strict 模式要求每一步声明 `scope_refs`；H0 检查其为 TaskSpec data scope 子集。H1 再用工具
  `scope_resolver(arguments)` 验证声明与真实参数一致。
- secret 只能通过受控引用传递，不得写入 TaskSpec、scope 或事件。原始 arguments 仍受数据库 RLS
  保护，事件和 API 输出继续按 `sensitive_fields` 脱敏。

### 3.3 `agent_tool_runs`

保留现有 `(tenant_id, tool_name, idempotency_key)` 唯一约束，并新增：

| 列 | 用途 |
| --- | --- |
| `run_id`、`task_id`、`step_id` | 将逻辑调用关联到任务和步骤；历史记录允许为空。 |
| `plan_version` | 此步骤首次进入计划的版本。 |
| `attempt` | 当前尝试次数；每次真实调用前递增。 |
| `state` | `reserved/running/succeeded/failed/reconciliation_required`。 |
| `started_at`、`completed_at` | 调用生命周期。 |

新增新任务的 `UNIQUE (task_id, step_id)`。每次 attempt 的详细开始、失败和结束仍写入
`agent_events`，避免为 H0 提前增加 attempt 历史表。

## 4. TaskSpec v1

TaskSpec 是服务端冻结的契约快照：

```json
{
  "schema_version": 1,
  "execution_mode": "strict",
  "success_criteria": [
    {
      "verifier": "retrieval_acl",
      "version": 1,
      "parameters": {"expected_source": "connector:git:repo-a"},
      "required": true
    }
  ],
  "data_scope": {"source_refs": ["connector:git:repo-a"]},
  "allowed_tools": ["sync_git", "rag_chat"],
  "limits": {"max_steps": 6, "deadline_seconds": 900},
  "created_by": "alice",
  "created_at": "2026-08-01T00:00:00Z"
}
```

- `execution_mode` 只能是 `strict` 或 `legacy`。
- strict 要求非空、结构化的 `success_criteria`；H0 验证字段格式，H1 注册并执行 verifier。
- legacy 由旧 `tool + arguments` 请求转换，只允许 `ToolSpec.side_effecting=False` 的单步骤工具。
- tenant、owner、role 仍由数据库列和服务端身份提供，不接受客户端字段。
- `allowed_tools`、审批点和调用者角色由 Tool Registry 与规范化计划推导后冻结。
- H0 只支持并执行 `max_steps`、`deadline_seconds`；未知 limit 拒绝，避免保存但不执行的假预算。

H0 为 `ToolSpec` 增加最小的 `side_effecting: bool = false`，只用于 legacy 兼容限制。完整读写范围、
补偿和结果契约在 H1 加入。

## 5. API 设计

### 5.1 创建任务

扩展 `POST /api/tasks`：

```json
{
  "goal": "从已授权来源生成支持窗口报告",
  "execution_mode": "strict",
  "steps": [
    {"tool": "sync_git", "arguments": {}, "scope_refs": ["connector:git:repo-a"]},
    {"tool": "rag_chat", "arguments": {"query": "Aurora 支持窗口"}, "scope_refs": []}
  ],
  "success_criteria": [
    {"verifier": "retrieval_acl", "version": 1, "parameters": {}, "required": true}
  ],
  "data_scope": {"source_refs": ["connector:git:repo-a"]},
  "limits": {"max_steps": 6, "deadline_seconds": 900}
}
```

规则：

1. `steps` 与旧 `tool/arguments` 二选一；混用时返回 400。
2. strict 必须提供 `steps`、success criteria、data scope 和 limits；legacy 只能走旧单步骤请求。
3. 写数据库前验证工具存在、当前角色、参数 schema、side-effect 兼容、步骤数与 scope 子集。
4. 服务端预生成 task/run/step ID 和幂等键，在同一事务写入任务与 `planned` 事件。
5. 运行时在每一步再次验证当前角色、ToolSpec、deadline、scope 声明、lease 和控制请求。

旧 WebUI 聊天继续使用 legacy `rag_chat`。现有文档导入、Git 同步、训练和发布等副作用入口必须
改为 strict TaskSpec；H1 verifier 完成前，它们最多进入 `awaiting_verification`，不能显示业务成功。

### 5.2 任务控制

新增或收紧：

- `POST /api/tasks/{id}/pause`：设置 `pause_requested`；若无工具运行立即 `paused`，否则进入
  `pausing`，工具返回并记录结果后暂停，不开始下一步。
- `POST /api/tasks/{id}/cancel`：设置 `cancel_requested`；若无工具运行立即 `cancelled`，否则进入
  `cancelling`。H0 不杀死 Python 线程，返回后记录可能产生的副作用再取消。
- `POST /api/tasks/{id}/replan`：只允许 owner/admin 在 `paused`、`waiting_approval` 或 `failed`
  且没有有效 lease 时调用；必须携带 expected `version`、替换后缀和 reason。

### 5.3 原子 replan

replan 在单事务中：

1. `SELECT ... FOR UPDATE` 并校验 tenant、owner/admin、expected version、状态和 lease；
2. 保留 `[0, current_step)` 前缀和其中所有 step ID；
3. 验证新后缀不增加 allowed tools、不扩大 TaskSpec data scope，生成新 step ID/key；
4. 递增 `plan_version`，清除 `approval_json`、pause/cancel 请求和旧 finish reason；
5. 状态统一转为 `paused`，由调用者显式 resume；
6. 写 `replanned` 事件，包含原因、操作者、旧/新版本、current step、规范化计划 hash 和脱敏后缀。

计划 hash 使用排序键和紧凑分隔符的 canonical JSON 后做 SHA-256。TaskSpec 本身不变；需要新工具、
新来源或新成功条件时创建新 task。

## 6. Lease、heartbeat 与执行循环

每次 `run()` 使用唯一 worker ID，通过单条条件 UPDATE 获取 lease：

```text
task 可执行
AND (lease 为空 OR 已过期 OR owner 是当前 worker)
AND version = expected_version
```

- lease 时长固定 30 秒，工具执行期间每 10 秒 heartbeat；先使用常量，真实长任务数据证明需要时再配置。
- 状态推进、current step 增加、审批和 replan 都使用 expected version；冲突时重新读取，不能覆盖。
- 正常安全停点释放 lease；进程崩溃后 lease 到期才允许接管。
- 接管前读取当前 step 的 tool run：
  - 无副作用且未成功：可以新 attempt；
  - 已成功：只推进 checkpoint，不再次调用；
  - 副作用处于 running/unknown：转 `reconciliation_required`；H0 不自动重放。
- H0 无法判断副作用工具抛错前是否已经改变外部系统，因此这类异常默认也进入
  `reconciliation_required`；只有 H1 提供确定性 reconciliation 后才允许自动重试。

deadline 使用 TaskSpec 创建时间加 `deadline_seconds`。超时在下一安全停点停止；已运行工具的真实结果
仍必须记录，不能因客户端超时而丢弃证据。

## 7. 事件模型

H0 新增或规范化以下事件，不为 heartbeat 逐次写事件：

- `planned`：run、TaskSpec schema、plan version、canonical plan hash、脱敏计划；
- `lease_acquired` / `lease_recovered`：worker 与过期 lease 信息；
- `tool_attempt_started` / `tool_attempt_failed` / `observed`：run/step/attempt 与脱敏结果；
- `control_requested`：pause/cancel、操作者与当时 step；
- `paused` / `cancelled`：实际到达安全停点后记录；
- `replanned`：原子 replan 证据；
- `awaiting_verification`：strict 计划已执行，等待 H1；
- `reconciliation_required`：禁止自动重放的原因。

事件不保存 access token、secret 内容或模型私有推理；只保存执行决策、结构化结果和来源引用。

## 8. 代码改动范围

| 位置 | H0 改动 |
| --- | --- |
| `src/storage/migrations/008_harness_task_contract.sql` | task/run/lease/control 字段、tool-run 关联、约束与索引；复用现有 RLS。 |
| `src/core/agent_runtime.py` | TaskSpec/PlanStep 校验、稳定 ID/幂等、lease/CAS、控制、安全接管和原子 replan。 |
| `src/core/runtime_tools.py` | 标记现有工具是否有副作用；不实现 H1 ToolResult/verifier。 |
| `webui/app.py` | strict/legacy 创建模型，pause/cancel/replan endpoint 与并发版本参数。 |
| `webui/static/*` | 显示 run、plan version、执行模式和真实控制状态；完整时间线留给 H3。 |
| `tests/test_agent_runtime.py` | 数据契约、并发、幂等、控制、replan、恢复、RLS 与轨迹覆盖。 |

## 9. 测试方案

实现完成前必须通过：

```bash
TEST_DATABASE_URL='<isolated-test-database-url>' \
  .venv/bin/pytest -q tests/test_agent_runtime.py tests/test_runtime_tools.py
.venv/bin/ruff check src/core/agent_runtime.py src/core/runtime_tools.py webui/app.py \
  tests/test_agent_runtime.py tests/test_runtime_tools.py
git diff --check
```

必测场景：

1. strict 两步任务使用同一 run、不同稳定 step ID，执行后停在 `awaiting_verification`。
2. legacy `rag_chat` 继续工作；legacy 副作用工具在执行前拒绝。
3. retry/replan 保留步骤不更换 idempotency key；替换步骤获得新 ID；不同 task 不共享 key。
4. 两个 worker 并发运行只有一个获得 lease；过期 lease 可接管，未过期 lease 不可抢占。
5. 副作用结果不确定时进入 `reconciliation_required`，不发生第二次调用。
6. pause/cancel 在工具运行期间显示过渡状态，工具返回后才到达真实安全停点。
7. waiting approval/failed replan 后统一为 paused，不出现无 approval 的 waiting 状态。
8. replan 不能修改已执行前缀、增加工具、扩大 scope 或绕过审批；CAS 冲突不覆盖新状态。
9. 普通用户不能操作他人任务，跨 tenant 无法读取 run、事件或 tool run。
10. 非法 VerifierSpec、deadline、limits、schema 或缺失 strict 成功条件在写库前失败且无副作用。

最小轨迹评测覆盖正常、审批拒绝、并发抢占、取消、恢复、replan 和跨 tenant 拒绝；轨迹同时断言
事件序列和数据库 outcome，而不是只断言最终文本。

## 10. H0 退出门禁

H0 只有全部满足时才能合并回 `feat/harness`：

- TaskSpec、run、step、attempt 与计划版本可从 PostgreSQL 查询并通过 tenant RLS 隔离；
- 并发 worker、retry、replan、pause、cancel 和进程失联不会制造重复副作用或虚假终态；
- strict 任务没有 verifier 时只能到 `awaiting_verification`，不能声称业务完成；
- 一条失败轨迹能解释执行者、计划版本、失败步骤、控制请求和安全恢复选择；
- 迁移可为历史任务回填且不破坏现有单步无副作用调用；
- 定向测试、Ruff、迁移验证和 `git diff --check` 全部通过。

H0 通过后，H1 才实现 scope resolver、独立 verifier、结构化 ToolResult、内容 trust label 与证据策略。
