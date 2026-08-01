# H1 设计：结构化工具结果与独立验证

> 状态：修订设计待批准。分支：`feat/harness-h1-verification`；基线：`feat/harness` 的
> H0 提交 `a40d988`。H1 只让 strict task 获得可验证的阶段结论；完整 MinIO evidence
> manifest、Kubernetes Job、跨存储恢复和 LLM judge 分别留给 H2/H5。

## 1. 目标、完成定义与非目标

H0 已能可靠执行 strict 计划，但结束状态只能是 `awaiting_verification`。H1 把工具 payload
转换为不可变 `ToolResult`，再由版本化、确定性、只读 verifier 给出阶段结论：

```text
approved step → gateway-owned ToolResult → required verifier gates
    → passed: checkpoint → next step / verified success
    → business failure: verification_failed → replan
    → verifier unavailable: verification_blocked → retry verification only
```

H1 不相信 handler 返回的 `status: completed`。外部文本、用户内容和工具输出统一视为
`untrusted_data`，不能改变计划、工具权限、TaskSpec scope、verifier 参数、系统提示或记忆策略。

H1 不实现新的 Connector、自动 memory distillation、异步训练/发布或完整证据包。训练、评测、
发布和 Spark 工具只完成 v2 契约声明并保持禁用，直到 H2/H5 具备对应 Job、证据和发布门禁。

## 2. 核心不变量

1. handler 只返回工具专属 payload；Tool Gateway 是 ToolResult envelope、状态和脱敏结果的权威。
2. terminal ToolResult 首次写入后不可覆盖；恢复只重跑只读 verifier，不重放已成功副作用。
3. TaskSpec 中的 criterion、工具/verifier 版本和参数创建后冻结；replan 只能复用，不能扩展。
4. required verifier 全部通过前不推进 checkpoint，也不启动下一个副作用步骤。
5. verifier 的代码接口、数据库角色和对象存储凭据均不可写被验证对象。
6. 业务不满足与 verifier 基础设施故障使用不同状态和恢复路径。
7. 每次验证尝试都追加保存，并绑定精确 ToolResult 与 verifier contract digest。
8. H1 成功只证明 PostgreSQL 中记录的阶段事实；H2 才提供完整跨存储、可回放 evidence manifest。

## 3. TaskSpec、criterion 与 replan

### 3.1 `VerifierSpec` v1

TaskSpec 的 `success_criteria` 规范化为不可变 criterion registry：

```json
{
  "criterion_id": "document-indexed",
  "verifier": "verify_ingest",
  "version": 1,
  "contract_digest": "sha256:...",
  "parameters": {"max_rejected": 0},
  "phase": "after_step",
  "required": true
}
```

- `criterion_id` 在一个 TaskSpec 内唯一，由客户端提供业务稳定名称，服务端校验格式和唯一性。
- `phase` 只能是 `after_step` 或 `final`。前者在关联步骤后执行；后者在全部步骤完成后执行。
- `required=true` 失败会阻断；optional criterion 失败只记录 `verification_warning`，不能掩盖 required
  结论。
- verifier、版本、参数与 contract digest 在创建时冻结；请求不得提供未知 verifier 或版本。

每个规范化 PlanStep 增加 `verifier_refs: [criterion_id]`。请求可以按 criterion ID 关联，而不是绑定
服务端生成的 `step_id` 或易变的 `step_index`。strict 的副作用步骤至少关联一个 required
`after_step` criterion；无副作用步骤可以为空。

### 3.2 replan 规则

replan 保持 TaskSpec 不变：

- 新后缀只能引用 TaskSpec 已有 criterion，不能新增、删除或修改 verifier 参数；
- 已通过 checkpoint 前缀及其 step ID、ToolResult 和 verifier 记录保持不变；
- 被替换步骤获得新 step ID，可以重新关联尚未满足的既有 criterion；
- 一个 required `after_step` criterion 在新计划中必须且只能被一个未完成步骤引用；
- final criterion 不绑定步骤；新计划仍必须能够满足全部 required final criteria。

旧 H0 strict task 没有 `criterion_id/verifier_refs`，继续停在 `awaiting_verification`，不能被 H1
自动提升为成功。需要验证时由用户创建新的 H1 strict task。

## 4. `ToolSpec` v2 与 scope 模型

保留 H0 字段并增加：

| 字段 | 规则 |
| --- | --- |
| `version` / `contract_digest` | 正整数版本和 canonical contract SHA-256；冻结到 TaskSpec/PlanStep。 |
| `scope_resolver(arguments, identity)` | 只根据可信参数和身份解析最大可能访问范围，不读取工具输出。 |
| `read_scope` / `write_scope` | 静态 capability 标签，用于注册和审计。 |
| `reversible` / `reconcile` | 副作用是否可补偿；reconcile 只查询既有 operation 状态。 |
| `result_validator(payload)` | 验证工具专属 payload；不新增动态 JSON Schema 框架。 |
| `expected_artifacts` | 允许的 store/kind、最大数量和 hash 要求。 |
| `result_sensitivity` | 输出 JSON path 对应 `public/internal/secret`；未分类字段 fail closed。 |

scope 使用规范化字符串词汇表，例如 `raw:document:<key>`、`connector:git:<repo>`、
`knowledge:tenant:<tenant_id>`，并区分：

- `declared_scope`：PlanStep 声明的最大范围；
- `resolved_scope`：scope resolver 在执行前从参数和身份解析出的最大范围；
- `observed_scope`：ToolResult 中工具实际访问/产生的 refs，经 verifier 检查。

执行前要求 `resolved_scope == declared_scope`，且二者是 TaskSpec data scope 的子集；执行后要求
`observed_scope` 是 declared scope 的子集。`rag_chat` 的 resolved scope 是授权知识域，而不是尚未
发生的实际召回文档；实际引用写入 observed scope。

## 5. `ToolResult` v1

handler 只返回 `payload`。Gateway 校验 payload 后构造并写入 `agent_tool_runs.result_json`，最大
64 KiB：

```json
{
  "schema_version": 1,
  "status": "succeeded",
  "tool": {"name": "ingest_document", "version": 2, "contract_digest": "sha256:..."},
  "input_refs": ["raw:document:pilot.md"],
  "observed_scope": ["raw:document:pilot.md"],
  "output": {"document_ids": ["..."], "chunk_count": 3},
  "artifacts": [
    {"store": "postgres", "kind": "document", "id": "...", "version": 1, "sha256": "..."}
  ],
  "metrics": {"accepted": 1, "rejected": 0},
  "operation_ref": null,
  "log_ref": null,
  "failure": null,
  "next_action": "verify",
  "recorded_at": "2026-08-01T00:00:00Z"
}
```

- 状态只能是 `succeeded`、`failed`、`reconciliation_required`。
- handler 抛错时 Gateway 合成 `{category, code, redacted_message}` failure；handler 无权返回最终
  verifier 结论。
- artifact 使用结构化 store/kind/id/version/hash，不使用含义不清的伪 URI。
- Git Connector 自身的 UUID 改称 `connector_run_id`，在 `operation_ref` 中保存；harness `run_id`
  始终来自 agent task，二者不得混用。
- 无 ToolResult、payload schema 不符、scope 越界、artifact 数量/类型/hash 不符或结果超限时，
  Gateway 写失败结果，任务不得推进。
- terminal ToolResult 只允许 `WHERE result_json IS NULL` 的首次写入；后续调用读取相同 result digest。

## 6. Verification 记录与 digest

迁移 `009_harness_verification.sql` 新建 `agent_step_verifications`。不重复保存 `run_id`，通过
`task_id → agent_tasks.run_id` 查询，避免 task/run 配对不一致。

| 列 | 规则 |
| --- | --- |
| `verification_id` UUID PK | 服务端生成。 |
| `tenant_id`, `task_id`, `step_id`, `criterion_id` | 关联 task、步骤和冻结 criterion。 |
| `verifier`, `verifier_version`, `verifier_contract_digest` | 实际执行的注册表版本。 |
| `attempt` | 从 1 递增；每次验证都追加记录。 |
| `status` | `passed`、`failed`、`blocked`、`warning`。 |
| `tool_result_digest`, `input_digest` | 绑定 ToolResult 和 canonical verifier 输入。 |
| `error_code`, `summary_json` | 固定错误码及脱敏、≤16 KiB allowlist 摘要。 |
| `started_at`, `completed_at` | 验证生命周期。 |

唯一键为 `(task_id, step_id, criterion_id, attempt)`；另建 run 查询索引时通过 task join。表启用并
强制 tenant RLS：任务 owner/admin 可读；仅 AgentRuntime 应用角色写 verifier 结论。H1 不建立
泛化 artifact 表；H2 再保存完整 verifier 输入、日志和 evidence manifest。

`input_digest` 对以下 canonical JSON 计算 SHA-256：task ID、step ID、criterion、TaskSpec hash、
ToolSpec version/hash、ToolResult digest、artifact refs/hash 与非敏感 verifier 参数。

## 7. Verifier 隔离与最小检查器

`VerifierRegistry` 注册 `(name, version, contract_digest, timeout_seconds, max_attempts, handler)`；
重复 name/version、无版本或可变 contract 注册失败。handler 通过 `asyncio.to_thread` 加 timeout
执行，使 H0 heartbeat 和取消检查继续运行。

```text
verify(spec, task_view, step_view, tool_result, read_only_services) -> VerificationResult
```

隔离不是只靠接口约定：

- PostgreSQL verifier 连接使用独立只读角色并执行 read-only transaction；
- MinIO verifier 凭据只允许 GetObject/HeadObject；
- verifier 不获得 Coordinator、ToolRegistry、任务写接口或应用数据库连接；
- verifier 返回结论后，由 AgentRuntime 使用应用角色原子写 verification、event 和 checkpoint；
- 在线 verifier 不模拟任意 tenant/role。跨 tenant 与无 ACL 不可见性由隔离集成测试证明。

| verifier | H1 确定性结论 |
| --- | --- |
| `verify_ingest@1` | document IDs 属于 tenant、状态 ready、source/version/hash 匹配；chunk/rejected 计数一致；固定敏感规则版本无命中。 |
| `verify_retrieval@1` | 以 task identity 和冻结 query 检查 expected document/chunk 可召回，引用均属于 declared scope 且满足现有 ACL。 |
| `verify_memory@1` | 只读检查来源 event、TTL、scope、状态、hash 重复和既有冲突；不创建或批准 memory。 |
| `verify_release@1` | 只读检查已有 release record 的固定评测、guardrail、rollback target 和 manifest hash；不触发发布。 |

`verify_retrieval` 不接受 SQL、tenant、role 或任意 identity 参数。敏感扫描只使用版本化的确定性规则，
规则版本写入 verifier contract；H1 不使用 LLM 判断敏感性或批准发布。

## 8. 状态机、原子性与恢复

每个 step 的顺序：

1. 校验 role、ToolSpec version/hash、scope、lease、deadline、approval 和控制请求；
2. 调用工具并首次持久化 immutable ToolResult；
3. 依次执行该 step 的 verifier refs；每次结论先追加 verification attempt；
4. 在一个短数据库事务中原子写最终 verification event、task state 和 checkpoint；
5. required verifier 全部 passed 后才增加 `current_step`，随后开始下一步骤；
6. 计划完成后执行 final criteria，全部 required passed 才进入 `succeeded`。

状态与恢复：

- `verification_failed`：业务事实确定不满足；不可直接 retry，只能 replan 或新建 task；
- `verification_blocked`：verifier timeout/依赖不可用；可通过新增 `POST .../verify` 仅重试验证；
- `verification_warning` 只作为事件，不成为 task state；
- verifier crashed/timeout 映射为 blocked，不得伪装为业务 failed；
- 工具结果已保存、verification 未完成时，恢复读取 ToolResult 并继续 verifier，不调用 handler；
- verifier passed 但 checkpoint 事务未提交时，重复只读验证并追加 attempt 是安全的；
- pause/cancel 在 verifier 返回后的安全点生效，不会删除已记录的验证事实。

`verification_failed`、`verification_blocked` 加入 stop states；replan 允许二者，并保留全部已验证
checkpoint。`/verify` 必须携带 expected task version，仅允许 blocked 或仍在验证中的当前步骤。

## 9. 信任、脱敏与保留

- ToolResult、日志、外部文本和 verifier 输入默认 `untrusted_data`，仅作为 typed data 传递；运行时
  不存在从其解析工具调用、verifier refs 或计划变更的代码路径。
- sensitivity 使用 JSON path；`secret` 永不进入事件/API，`internal` 仅在 tenant RLS 下返回，
  `public` 才进入 UI。未分类输出字段拒绝写入，而不是默认公开。
- verifier summary 采用固定 allowlist，不复制原始工具输出。事件不保存原始文档、token、授权头、
  私有推理或未经脱敏的异常。
- ToolResult 与 verification 行按任务审计保留；删除传播与完整 evidence retention 在 H2 定义。

## 10. 现有工具的 H1 可用性

| 工具 | H1 状态 | 契约与 verifier |
| --- | --- | --- |
| `ingest_document` | 启用 | ToolResult v1、verify_ingest、verify_retrieval。 |
| `sync_git` | 启用 | connector_run_id、计数/hash、verify_ingest、verify_retrieval。 |
| `rag_chat` | 兼容只读 | 输出 answer/citations 与 observed scope；不能单独证明业务完成。 |
| `verify_memory` | 只读 verifier | 验证已有 memory，不启动 memory 写入。 |
| `verify_release` | 只读 verifier | 验证已有 release record，不启动发布。 |
| `ingest` / `train` / `evaluate` / `release` | `blocked_pending_h2_h5` | 注册 v2 contract，但 strict/legacy 调用均明确拒绝。 |

这作为总执行计划中“迁移现有工具”的 H1 解释：完成契约迁移不等于允许执行。异步 Spark/训练和发布
在 H2/H5 获得 Job、artifact、评测与回滚证据后才能解除 blocked。

## 11. API、UI 与代码范围

| 位置 | H1 改动 |
| --- | --- |
| `src/storage/migrations/009_harness_verification.sql` | verification attempt 表、RLS、约束与应用/verifier 角色权限。 |
| `src/core/agent_runtime.py` | ToolResult、scope、per-step/final verifier gate、新状态、`/verify` 恢复。 |
| `src/core/verifiers.py` | 最小 registry、只读服务和四个确定性 verifier。 |
| `src/core/runtime_tools.py` | 工具 payload 适配、ToolSpec v2、禁用高风险同步入口。 |
| `webui/app.py` | typed criterion/task 请求、verification 查询与重试 API、脱敏响应。 |
| `webui/static/script.js` | 显示 result digest、verifier/version、attempt、结论和 failure code。 |
| `tests/test_agent_runtime.py`, `tests/test_verifiers.py` | 状态、恢复、ACL、scope、污染、版本和不可变性轨迹。 |

新增只读 `GET /api/tasks/{task_id}/verifications` 和受 CAS 保护的
`POST /api/tasks/{task_id}/verify`。不增加人工“批准 verifier”接口；人工只能批准工具调用或通过
replan/新任务改变执行路径。

## 12. 实施顺序与退出门禁

实施顺序：

1. migration、RLS、verification repository 和只读 verifier 角色；先验证权限隔离。
2. criterion/verifier refs、ToolSpec contract digest、ToolResult/VerificationResult 与最小 validator。
3. `ingest_document`、`sync_git` payload 与 scope；实现 verify_ingest/verify_retrieval。
4. runtime verifier gate、blocked retry、失败停止、checkpoint 原子性和 final criteria。
5. verify_memory/verify_release 的只读检查、API/UI 摘要和完整定向轨迹。

退出前必须验证：

- 工具伪报、缺失/错误 artifact hash、scope 不一致、ACL 错误、敏感命中、固定查询失败和 contract
  version 漂移均不能成功，后续副作用不启动；
- 工具结果写入后崩溃、verifier 通过但 checkpoint 未提交、verifier timeout 三种恢复均不重放工具；
- replan 只能复用冻结 criterion；required/optional 和 after_step/final 语义均有轨迹测试；
- verifier 数据库/Object Storage 凭据无法修改 document、memory、release、task 或原始对象；
- ToolResult 不可覆盖，每次 verification attempt 可追溯，跨 tenant 结果/结论不可读；
- prompt injection 字符串只能作为数据，无法改变计划、scope、criterion 或工具调用；
- 成功 run 可从 PostgreSQL 查询每步 ToolResult digest、verifier contract/attempt、结论和脱敏摘要；
- `ingest_document` 单文档闭环可真正进入 `succeeded`；高风险同步入口保持明确 blocked；
- 隔离 PostgreSQL 迁移、定向 pytest、Ruff、API schema、`git diff --check` 全部通过。
