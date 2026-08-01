# Agent Harness 执行计划

> 本计划执行 [Agent Harness 待办清单](./TODO.md)。目标是在不引入第二个运行时的前提下，
> 将当前 PostgreSQL `AgentRuntime` 演进为可验证完成复杂任务的单一权威路径。

## 执行规则

- 每个工作包在独立功能分支完成，经过测试、文档和退出门禁后再进入下一包。
- 复用现有 `agent_tasks`、`agent_events`、`agent_tool_runs`、PostgreSQL RLS、MinIO 与
  Tool Gateway；只在现有记录不足以表达 `run_id`、产物或验证结论时新增表。
- 任何带副作用的阶段都必须先有可恢复 checkpoint；任何“成功”都必须有执行器之外的验证证据。
- 不用模拟的 Spark、LoRA 或发布结果关闭真实门禁；硬件、模型或外部团队缺失时状态为 blocked。
- 每个工作包都同步增加最小 capability/regression 轨迹评测；H5 负责规模化，不是第一次建立评测。
- 同一 task 同一时刻只允许一个执行 lease；暂停、取消、超时和 Worker 丢失必须有明确终态或恢复路径。

## 工作包 H0：基线与任务契约

**状态：** 已完成工程退出门禁；实现和验证记录见
[H0 退出报告](./harness/H0_EXIT_REPORT.md)。H1 的 verifier 尚未实现，因此 strict 任务会停在
`awaiting_verification`，不会声称业务成功。

**目标：** 让一个任务在执行前具有机器可读、可审计的完成定义。

详细设计见 [H0 任务契约设计](./harness/H0_TASK_CONTRACT_DESIGN.md)。

1. 建立 `TaskSpec` 数据模型：目标、类型化 `VerifierSpec` 成功谓词、允许工具、数据范围、tenant、
   预算、超时、审批点和计划版本。自然语言描述只用于展示，不能单独关闭任务。
2. 为每个计划步骤生成跨 replan/retry 稳定的 `step_id`；副作用幂等键使用
   `<run_id>:<step_id>`，retry 只增加 attempt。结果不确定时先 reconciliation，不能直接重放。
3. 扩展任务创建 API，支持一次提交多个步骤；旧单步请求显式进入 `legacy` 模式并只允许无副作用
   工具，复杂任务必须使用 `strict` 模式和成功谓词。
4. 为任务生成 `run_id`，并让 `agent_tool_runs` 记录 `run_id`、`task_id`、`step_id`、attempt、
   state 和开始/结束时间。
5. 增加执行 lease、heartbeat、基于现有 `version` 的 compare-and-swap 和 `cancel_requested`，
   防止两个 API/Worker 并发执行同一 task。
6. 将 `replanned` 改为原子计划修订：只替换未执行后缀，记录理由、版本和前后 hash，失效审批
   清除后转为 `paused`/`created`，不得留下无审批内容的 `waiting_approval`。
7. TaskSpec 的 data scope 在 H0 禁止扩大；H1 通过工具 `scope_resolver` 验证实际参数范围。

**验收：**

- 多步骤任务在每一步审批后继续执行，且每个步骤的 run/task/step/attempt、计划版本和幂等键可查询。
- 并发执行只有一个 lease owner；Worker 失联后可安全接管，取消请求不会只改数据库而让副作用继续失控。
- replan 后保留步骤不重复执行；非法工具、超预算、scope 扩大或缺失严格成功谓词在执行前拒绝。
- 建立第一组轨迹评测：正常、审批拒绝、并发抢占、取消、恢复、replan 与跨 tenant 拒绝。

## 工作包 H1：工具结果、产物和独立 verifier

**状态：** 详细设计待批准，见 [H1 结构化结果与验证设计](./harness/H1_VERIFICATION_DESIGN.md)。

**目标：** 将“工具返回成功”替换为“可验证的阶段结论”。

1. 扩展 `ToolSpec`：最小角色、`scope_resolver`、读写范围、可撤销/补偿性、超时/重试/成本预算、
   预期产物、结果 schema 和 reconciliation/status 查询。
2. 规定统一 `ToolResult`：状态、输入版本、输出计数、产物 URI/hash、日志引用、失败分类和
   下一步建议；迁移 `ingest`、`sync_git`、`train`、`evaluate`、`release`。
3. 建立版本化 verifier registry；verifier 使用只读或隔离凭据，不能修改被验证产物，并优先使用
   确定性代码检查。实现最小 verifier 集：
   - `verify_ingest`：清洗后的记录数、拒绝原因、hash 和敏感内容检查；
   - `verify_retrieval`：document/chunk、ACL、tenant 隔离及固定查询证据；
   - `verify_memory`：来源、有效期、范围、状态和重复/冲突规则；
   - `verify_release`：固定评测、guardrail、回滚目标和 manifest。
4. 将 verifier 作为后续副作用前的必经步骤；验证失败使 run 停在可诊断状态，而非继续执行。
5. 外部文本、工具返回和用户内容带 trust label；把它们当数据而非指令。未经验证的内容不得扩大
   工具权限、data scope、系统提示或记忆写入策略。
6. 为 ToolResult、日志和 verifier 输出定义字段级敏感性、结构化脱敏、大小上限和保留策略。

**验收：**

- 执行器伪报成功、缺失产物、错误 ACL 或不达评测阈值时 verifier 必须失败。
- 任务详情可显示输入、产物、verifier 结论和失败原因，不再只显示 `completed`。
- 间接 prompt injection、工具越权和 verifier 修改产物必须被轨迹测试拒绝。

## 工作包 H2：统一运行证据与恢复

**目标：** 用一个 `run_id` 连接异步 Job 和全部可回放证据。

1. 为 run 维护不可变 manifest：TaskSpec、输入版本、每步 ToolResult、verifier、审批、
   checkpoint 与最终结论；PostgreSQL 保存索引，MinIO 保存按 hash 的完整证据包。
2. 写入 Harness fingerprint：Git commit、容器 image digest、migration、模型/tokenizer、prompt、
   Context/Skill、ToolSpec、verifier、依赖锁和非敏感配置版本；不保存模型私有推理过程。
3. manifest 使用 `staged → verified → published` 原子发布；定义 PostgreSQL/MinIO 部分成功后的
   outbox/reconciler、完整性校验、tenant ACL、加密、保留期和删除传播。
4. 将 Spark/Operator Job 作为受控异步工具：返回 job handle，持续回写 heartbeat、状态、日志和产物，
   不再由 Kubernetes 注解绕过 `AgentRuntime`。
5. 为异步 Job 定义取消、超时、孤儿接管和补偿；不可逆操作进入人工处置状态，不能盲目重试。
6. 将训练 Job、评测报告、adapter 和发布记录纳入同一 run；旧 full-cycle 仅保留为迁移期间的
   兼容入口，随后删除或改为创建受控 run。
7. 支持从最近通过 verifier 的 checkpoint 恢复；不重放已完成的不可撤销操作。

**验收：**

- 故意让清洗、索引或评测失败，后续步骤不得执行；修复后只从最后一个已验证 checkpoint 恢复。
- 给定 `run_id` 可重放输入版本、工具调用、审批、产物 hash、日志和验证结论。
- 故意制造数据库/MinIO 部分写入、Worker 失联和取消竞态，reconciler 能收敛到唯一可解释状态。

## 工作包 H3：完整产品闭环与外部输入

**目标：** 让用户在 WebUI 看到真实复杂任务，而不是单文档 smoke test。

1. 增加 run 详情时间线：任务目标、阶段、状态、输入/输出计数、证据、日志、审批、失败与恢复。
2. 以脱敏 PDF/DOCX 为首个样例，走 `Connector → Spark rough clean → refine/synthesis →
   verifier → PostgreSQL → RAG`；保留单文档路径仅用于诊断。
3. 接入一个只读外部业务来源（优先已实现 Git；Jira/Confluence 仅在 ACL、删除同步和测试齐备后启用）。
4. 提供一条跨来源信息冲突样例：显示来源、时间、ACL、权威规则、自动/人工裁决与最终回答。
5. 所有外部来源默认不可信；演示必须包含一条间接 prompt injection 样例并证明它无法触发工具、
   改写 scope 或进入长期记忆。

**验收：**

- WebUI 中一个 `run_id` 可完整展示数据、检索、反馈、记忆、训练候选、评测和发布门禁。
- 没有来源 ACL、验证或审批的内容不能进入检索、记忆或训练候选。

## 工作包 H4：Context、记忆与冲突治理

**目标：** 让 Agent 获得最小充分上下文，并安全地从会话中形成长期记忆。

1. 建立版本化 Context/Skills 包：任务目标、数据源契约、工具规则、已知限制、成功样例和
   失败处置；按任务选择性装载并记录版本。
2. 定义上下文生命周期：token 预算、append-only transcript、compaction 触发条件、摘要来源、
   context reset、结构化 handoff，以及恢复后重新验证身份、TaskSpec 和计划版本。
3. 在会话结束或轮数阈值触发 memory distillation，产出带来源、置信度、TTL、trust label 和
   策略标签的候选；compaction 不得成为唯一长期事实来源。
4. 实施分级写入：低风险个人记忆自动生效；跨用户/组织/高风险记忆由指定管理员审批；
   敏感类别默认拒绝。
5. 实施去重、冲突检测、supersede、证据展示、修订、删除和自动记忆开关；修复当前审批 API 的
   tenant 内任意用户审批边界。

**验收：**

- 自动摘要不泄露跨 tenant 内容，能过期、撤销和追溯到聊天事件。
- 冲突事实不被静默合并；无权威规则时必须显示冲突并等待人工裁决。
- context reset 后能从结构化 handoff 继续任务，且不会恢复已经撤销的权限或过期计划。

## 工作包 H5：轨迹评测、LoRA 与受控发布

**目标：** 用真实轨迹验证 harness，而非只测试单个函数。

1. 汇总 H0--H4 已建立的轨迹评测，分为 capability 与接近 100% 通过的 regression 套件；覆盖
   工具选择/不选择、ACL、污染、冲突、失败恢复、审批、证据完整性和成本/延迟。
2. 将 run 级反馈、verifier 失败、人工裁决和用户评价关联；仅合规且已审核的数据形成训练快照。
3. 将训练入口改为强制检查训练快照、固定基线评测、adapter manifest 和批准的发布计划。
4. 以同一 run 记录 candidate、shadow、canary、promote/rollback；LLM judge 只能作为有固定 rubric
   和人工标注校准的辅助 verifier，不能单独批准发布。
5. 每个任务运行多次 trial，保存 transcript 与环境 outcome；定期人工阅读失败轨迹，防止 grader
   过度刚性、被投机绕过或与真实业务价值脱节。

**验收：**

- 轨迹回归可捕获错误工具选择、越权、证据缺失和失败后继续执行。
- LoRA 未通过基线、安全或人工发布门禁时，adapter 不得加载到服务路径。

## 工作包 H6：试点运维与 GA

**目标：** 让试点可重复部署、隔离重置并获得外部证据。

1. 提供显式确认的测试环境 reset：只允许预注册的测试数据库、MinIO 前缀、Redis 前缀和 k3d 集群。
2. 在目标 IdP 完成 OIDC、role claim、tenant RLS、审计留存和恢复演练。
3. 用两支独立团队运行四周真实任务；每周审计 run evidence、价值指标、安全事件和未解决冲突。

**验收：**

- reset 不可能接受生产/共享资源作为目标；恢复演练不写源库。
- `GA-01` 的四周真实试点、周度审计和双方签署全部完成后，才关闭正式发布门禁。

## 推荐实施顺序

`H0 → H1 → H2 → H3 → H4 → H5 → H6`。H3 可在 H2 后开始准备试点样例，但不得绕过 H0--H2
的任务、证据和 verifier 要求；H4/H5 的自动化能力不得早于其对应证据和权限门禁。

## 分支与合并规则

- `feat/harness` 是 H0--H6 的集成分支；每个 `feat/harness-hN-*` 必须从其最新版本创建。
- 工作包退出门禁全部通过后，以非破坏方式合并回 `feat/harness`，再从更新后的集成分支创建下一包。
- 合并前记录测试、迁移、轨迹评测和未关闭的真实外部门禁；不得用后续工作包承诺替代当前退出条件。
