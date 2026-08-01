# H1 设计：结构化工具结果与独立验证

> 状态：设计待批准。分支：`feat/harness-h1-verification`；基线：`feat/harness` 的
> H0 提交 `a40d988`。本工作包只让 strict task 获得可验证的完成结论，不实施 H2 的完整
> MinIO evidence manifest、Kubernetes Job 或 LLM judge。

## 1. 目标与边界

H0 已能可靠地执行严格计划，但其结束状态只能是 `awaiting_verification`。H1 将每个工具的
执行事实规范化为 `ToolResult`，然后由版本化、确定性、只读的 verifier 形成结论：

```text
approved tool step → ToolResult persisted → required verifiers
    → passed: next step / succeeded
    → failed: verification_failed (stopped, diagnosable, resumable only by replan)
```

H1 不相信工具返回的 `status: completed`，也不让 verifier 修改被验证对象。外部文本和工具输出是
`untrusted_data`，只能作为 verifier 输入，不能变成任务指令、扩大 TaskSpec scope、修改系统提示或
直接写入记忆。

非目标：完整 run manifest 与跨存储恢复（H2）、自动 memory distillation（H4）、LLM-as-judge
（H5）和新的数据 Connector（H3）。

## 2. 最小数据契约

### 2.1 `ToolSpec` v2

保留 H0 字段，新增以下冻结到 TaskSpec/事件快照的元数据：

| 字段 | 规则 |
| --- | --- |
| `scope_resolver(arguments)` | 必须返回声明的 source refs；strict 执行前与 step `scope_refs` 精确比较，不能仅比较子集。 |
| `read_scope` / `write_scope` | 静态字符串集合，用于审计；H1 不支持运行时扩大。 |
| `reversible` / `reconcile` | 副作用工具必须显式声明是否可补偿；`reconcile` 仅返回状态，不写入外部系统。 |
| `result_schema` | ToolResult 的 `output` 子对象 schema；未知字段拒绝。 |
| `expected_artifacts` | URI 前缀、可选 hash、最大数量；只描述，不读取凭据。 |
| `result_sensitivity` | 字段级 `public/internal/secret`；secret 不进入 events/API。 |

现有工具的 H1 映射：

| 工具 | scope resolver | ToolResult / required verifier |
| --- | --- | --- |
| `ingest_document` | `raw/documents/<key>` | document id、chunk count、source hash → `verify_ingest`、`verify_retrieval` |
| `sync_git` | connector/repository ref | cursor、accepted/rejected/deleted counts、manifest ref → `verify_ingest`、`verify_retrieval` |
| `rag_chat` | 当前检索 source refs | answer 与引用数 → 无业务完成 verifier；仅供后续步骤读取 |
| `ingest` / `train` / `evaluate` / `release` | H1 明确拒绝启动 | H2/H5 接入 Job、评测和 release verifier 后才启用 |

这避免把现有同步执行的训练/发布包装成“已验证”。

### 2.2 `ToolResult` v1

所有 `agent_tool_runs.result_json` 写入同一对象，最大 64 KiB（沿用工具结果上限）：

```json
{
  "schema_version": 1,
  "status": "succeeded",
  "input_refs": ["raw/documents/pilot.md"],
  "output": {"document_ids": ["..."], "chunk_count": 3},
  "artifacts": [{"uri": "postgres://documents/...", "sha256": "...", "kind": "document"}],
  "metrics": {"accepted": 1, "rejected": 0},
  "log_ref": null,
  "failure": null,
  "next_action": "verify"
}
```

允许状态仅为 `succeeded`、`failed`、`reconciliation_required`。`failure` 为
`{category, message}`；message 先按敏感性规则脱敏再落库。H1 不保存原始文档内容、token、
授权头或模型推理链。无 `ToolResult`、schema 不符、产物引用不在允许前缀或 hash 缺失时，工具调用
视为失败，不能推进 checkpoint。

### 2.3 Verifier 记录

迁移 `009_harness_verification.sql` 新建单一 `agent_step_verifications` 表：

| 列 | 规则 |
| --- | --- |
| `verification_id` UUID PK | 服务端生成。 |
| `tenant_id`, `task_id`, `run_id`, `step_id` | 与 H0 task/step 关联；`task_id/step_id/verifier/version` 唯一。 |
| `verifier`, `verifier_version` | 注册表名称与不可变整数版本。 |
| `status` | `passed`、`failed`、`blocked`。 |
| `summary_json` | 脱敏、≤16 KiB 的结构化发现、计数、artifact refs 与失败码。 |
| `verified_at` | 服务器时间。 |

表启用并强制 tenant RLS，策略与 `agent_events` 相同：任务 owner 或 admin 可读；写入必须具有同一
tenant 且关联可见 task。H1 不新建泛化 artifact 表；H2 再将 artifact hash、完整 verifier 输入和
manifest 固化到 MinIO。

## 3. Verifier registry 与四个最小检查器

`VerifierRegistry` 只接受 `VerifierSpec(name, version, parameters)`，重复注册失败。每个 verifier
为同步、无网络写入的函数：

```text
verify(spec, task, step, tool_result, identity, read_only_services) -> VerificationResult
```

`read_only_services` 只暴露 PostgreSQL 只读查询和对象元数据/head；没有 Coordinator、ToolRegistry、
写凭据或任务控制对象。注册时拒绝 coroutine、可变 handler 或没有版本的 verifier。

| verifier | 确定性结论 |
| --- | --- |
| `verify_ingest@1` | ToolResult 的 document IDs 均存在、属于 tenant、状态 `ready`、source/hash 与 input ref 一致；chunk 总数和 rejected 原因与结果相符；敏感内容扫描未命中。 |
| `verify_retrieval@1` | 以 task identity 执行固定 query，期望 document/chunk 可被 owner/ACL 主体召回；跨 tenant 或无 ACL identity 必须不可见。 |
| `verify_memory@1` | 只读检查来源 event、TTL、scope、状态以及既有 hash 冲突/重复；H1 只提供验证，不自动写 memory。 |
| `verify_release@1` | 只读检查 release manifest 的固定评测通过、guardrail、rollback target；H1 不触发 release。 |

`verify_retrieval` 的参数包含固定 query、expected document/source 和预期可见/不可见 identity ref，
不接受用户提供的 SQL、角色或 tenant。所有参数由 TaskSpec 在创建时冻结。

## 4. 运行时状态与执行顺序

1. H0 在调用工具前重新检查 role、schema、lease、deadline；H1 同时运行 `scope_resolver`，其结果必须
   与该 step 的 `scope_refs` 相等。
2. 工具返回后，运行时把返回值适配并校验为 `ToolResult`，先写 `agent_tool_runs`，再写
   `tool_result_recorded` 事件。副作用不确定性仍优先进入 `reconciliation_required`。
3. 对该 step 的 required `VerifierSpec` 逐一执行，并在同一 checkpoint 事务写入 verifier 行与
   `verification_passed/failed/blocked` 事件。
4. 任一 required verifier `failed`：task 转为 `verification_failed`，释放 lease，不增加
   `current_step`，不开始下一工具。`blocked` 同样停在 `verification_blocked`，等待明确 replan。
5. 全部通过才推进 checkpoint。计划所有步骤完成时，strict task 转为 `succeeded`，`finish_reason`
   为 `verified_plan_completed`。

H1 将 `verification_failed` 和 `verification_blocked` 加入 stop/terminal 集合；replan 允许两者，但
必须保留已通过的前缀，替换失败步骤及之后后缀。已通过的 verifier 不能因 retry 被跳过或覆盖。

为避免 H0 TaskSpec 只有 task-level `success_criteria` 的歧义，H1 规定每个 criterion 增加
`step_id` 或 `step_index`；迁移兼容策略是旧 strict task 保持 `awaiting_verification`，不能被 H1
自动提升为成功。新 strict API 在创建时将 criterion 绑定到一个步骤；未绑定的 criterion 400 拒绝。

## 5. 信任、脱敏与权限

- 所有 `ToolResult.output`、日志摘要、外部文档片段和 verifier 输入默认 `untrusted_data`。
  它们不能调用工具、覆盖 `scope_refs`、改变 verifier 参数或直接形成 memory candidate。
- `secret` 字段写为 `"***"`，`internal` 仅随 tenant RLS API 返回，`public` 可出现在 UI 摘要。
  verifier summary 使用 allowlist 字段，不复用工具原始输出。
- verifier 只用调用者的 PostgreSQL RLS identity；用于反例 ACL 测试的 identity 由服务端从固定
  subject ref 解析，不能由请求体传入。没有对象写权限和 ToolRegistry 引用。
- 运行时拒绝 scope resolver 的异常、未知 source ref、输出超限、循环/嵌套非 JSON 值和 artifact
  URI 的未知 scheme。

## 6. API、UI 与迁移范围

| 位置 | H1 改动 |
| --- | --- |
| `src/storage/migrations/009_harness_verification.sql` | verification 表、RLS、索引和约束。 |
| `src/core/agent_runtime.py` | ToolResult 校验、scope resolver、每 step verifier gate、新状态与事件。 |
| `src/core/verifiers.py` | 最小 registry、只读服务门面和四个确定性 verifier。 |
| `src/core/runtime_tools.py` | ingest/document/git ToolResult 适配与 H1 ToolSpec v2；明确禁用尚无 verifier 的高风险工具。 |
| `webui/app.py` | 返回 task 的 verification summary；不得暴露 secret 字段。 |
| `webui/static/script.js` | 显示每个 step 的 result/verdict/failure code；H3 才做完整 run 时间线。 |
| `tests/test_agent_runtime.py`, `tests/test_verifiers.py` | 契约、ACL、伪报、污染、失败停止和脱敏轨迹。 |

新增只读 API：`GET /api/tasks/{task_id}/verifications`。不增加客户端“批准 verifier”接口；修复
输入或证据后必须通过 replan 创建新的未执行步骤。

## 7. 验收与实施顺序

按以下顺序实施，每步保持测试可运行：

1. 写 migration、RLS 和 verification repository；先测试 tenant/owner 隔离。
2. 添加 `ToolResult`/`VerificationResult` 数据类、JSON/sensitivity/schema validator 与 registry。
3. 改造 `ingest_document` 和 `sync_git`，实现 `verify_ingest`、`verify_retrieval`；其余两个 verifier
   仅验证已有只读记录，不启动训练/发布。
4. 将 verifier gate 接入 `AgentRuntime`，实现失败状态、不可跳过 checkpoint 与 strict `succeeded`。
5. 扩展 API/UI 摘要和定向轨迹测试。

退出门禁：

- 工具伪报、缺失 artifact/hash、scope 不一致、ACL 错误、敏感内容和固定检索 query 失败均使 strict
  task 不成功且后续副作用不启动。
- verifier 不能修改文档、memory、release 或 task；跨 tenant 的结果和结论不可读。
- 成功 run 可以从 PostgreSQL 查询每步 ToolResult、verifier/version、结论、失败码和脱敏摘要。
- `ingest_document` 的单文档闭环可在 H1 后真正进入 `succeeded`；训练、发布和异步 Spark 路径仍
  明确 blocked，留给 H2/H5。
- 隔离 PostgreSQL 迁移、定向 pytest、Ruff、`git diff --check` 全部通过。
