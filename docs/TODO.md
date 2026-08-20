# DataAlchemy Agent Harness 待办清单

> 目标不是堆叠 Agent 功能，而是让模型在明确上下文、受控工具和真实环境中完成复杂任务，
> 并用独立证据证明完成、失败或需要人工决策。当前不引入第二个运行时；以 PostgreSQL
> `AgentRuntime`、Tool Gateway、MinIO 产物和发布治理为唯一权威路径。

> **状态复核：2026-08-20，基线 `main`。** `[x]` 表示当前代码、测试或真实工程
> 证据已足以关闭该工程项；`[ ]` 表示尚未实现、只有部分实现，或仍需要真实数据/人工/外部
> 验收。H5/H6 的 synthetic 预演不会被标记为真实发布门禁通过。

## 完成标准

一个复杂任务只有同时具备下列证据才可称为完成：

```text
任务契约 → 受控执行 → 产物与状态 → 独立验证 → 可恢复/可回放决策
```

工具返回 `completed`、Kubernetes Job 成功或模型生成文本均不足以单独证明任务完成。

## P0：可验证执行核心

### 1. 任务契约与统一运行编号

- [x] 每个 **strict** 任务在执行前持久化 `TaskSpec`：目标、成功谓词、允许工具与数据范围、
  tenant、预算、超时、审批点、幂等键和计划版本；legacy 模式仅保留无副作用兼容入口。
- [x] 由 API 创建真正的多步骤计划；`Plan → Act → Observe → Replan` 的 replan 能
  产生受审计的新计划，不能只记录同名事件。
- [x] 为一次复杂任务分配唯一 `run_id`，关联 PostgreSQL 任务/事件、Spark Job、训练 Job、
  原始数据、评测报告、adapter 与发布记录。

### 2. 工具契约与受控环境

- [x] 每个受控工具声明输入 schema、最小权限、读写范围、速率/成本/重试预算、可撤销性、
  预期产物和成功/失败类型；副作用工具不再只返回模糊 `status: completed`。
- [x] Git 与 PDF/DOCX 外部输入已接入受限路径：显式身份/tenant、ACL、落地前校验、版本或
  source hash、审计和删除/替换语义。
- [ ] Jira、Confluence、网页/API 尚未形成可验收的外部 Connector；现有 Spark cleaner 不能
  代替连接器、源 ACL 和删除同步。
- [x] 长时或有副作用的工具在受限 Worker/Job 中运行；运行时只持有任务句柄和最小能力，
  工具失败不会丢失 tenant、审批或恢复语义。

### 3. 独立验证、恢复与证据包

- [x] 每个关键阶段由独立 verifier 验证，而非相信执行器自报成功：清洗质量与脱敏、
  文档/chunk/ACL 可检索性、记忆范围、LoRA 固定评测、发布 guardrail 与回滚目标。
- [x] 将输入版本、输出计数、产物 URI/hash、日志链接、verifier 结论、失败原因和人工决定
  写入不可变 run manifest；任务可从最近已验证 checkpoint 恢复。
- [x] 现有 Spark/Operator 已通过受控 Job 接入 run；旧 full-cycle 注解旁路被拒绝或保持禁用，
  不再绕过 `AgentRuntime` 和任务事件。

### P0 退出门禁

- [x] 一个故意失败的多步骤任务可显示失败阶段、保留已验证产物、拒绝未验证后续步骤，并从
  checkpoint 恢复。
- [x] 一个成功任务可从 `run_id` 重放所有输入、工具调用、产物、验证和审批结论。

## P1：Agent 可理解性与完整产品闭环

- [x] **Context / Skills 包**：按任务提供版本化目标、数据源契约、工具使用规则、已知限制、
  成功样例与失败处置；运行时只装载当前任务所需上下文。
- [ ] **产品完整闭环展示（部分完成）**：WebUI 已用一个 `run_id` 展示原始数据、Spark
  rough clean、refine、文档/chunk、RAG 验证、反馈和 evidence；Memory、training candidate、
  LoRA、evaluation、release 目前仍以保守 gate 状态展示，尚未全部串成同一 run 的动态证据时间线。
  设计见 [WebUI Run 与反馈治理设计](./harness/WEBUI_RUN_FEEDBACK_GOVERNANCE_DESIGN.md)。
- [x] **受控 PDF/DOCX 试点路径**：沿 WebUI document pilot → MinIO → Spark → verifier →
  PostgreSQL → RAG 的完整路径，不绕过接入、脱敏、tenant 和审计门禁直接写入检索表。
- [ ] **外部信息任务样例**：至少用一个真实只读业务任务验证自动抓取、来源比较、人工审批和
  报告产出；不以单文档检索 smoke test 代替。

## P2：上下文、记忆与冲突治理

- [x] **Memory distillation**：在会话结束或达到轮数阈值时提炼摘要、偏好、待办和程序性知识；
  每条记忆带来源事件、tenant、置信度、有效期和写入策略。
- [x] **分级写入与授权**：低风险个人记忆可自动生效；跨用户、组织规则和高风险内容进入指定
  管理员审批；密钥、权限、财务、人事内容默认拒绝。收紧当前任何同 tenant 用户可审批候选的边界。
- [x] **冲突处置与用户控制**：对文档、连接器版本和记忆保留来源、时间、ACL 与置信度；按明确
  权威规则自动 supersede，否则展示冲突证据并进入人工裁决。用户可查看、修订、删除、禁用自动
  记忆和设置保留期。

## P3：评测、学习与安全发布

- [x] **轨迹评测集（工程门禁）**：覆盖工具选择、ACL 越权拒绝、输入污染、冲突、失败恢复、
  人工审批和证据完整性；评测断言过程及副作用，而不只比较最终回答。
- [x] **在线质量闭环（权威索引）**：WebUI 反馈保留不可变 MinIO source，并按
  `run_id` 幂等写入 PostgreSQL `trajectory_annotations`；reviewer 可明确设置审核结果、
  `training_allowed`、训练用途与权限版本。H5 只从已审核的权威索引创建 snapshot。
  将这些状态完整展示在同一 WebUI 动态时间线仍属于上述“产品完整闭环展示”未完成项。
- [x] **LoRA 发布门禁（工程门禁）**：训练快照、基线对比、固定评测、shadow、canary 和回滚
  均关联同一证据包；训练入口强制前置条件。真实业务质量资格仍属于 P4/H6。

## P4：试点运维与正式发布

- [ ] **真实数据资格认证**：为授权、脱敏且代表目标任务分布的数据建立不可变 manifest，记录
  owner、tenant、ACL、用途、许可、保留/删除策略和 suite 隔离；synthetic 数据只能用于工程回归。
- [ ] **人工校准与候选资格**：复用 H5 annotation/evaluation，以独立 reviewer 校准 LLM judge，
  对失败轨迹和安全 case 做人工复核；缺少校准或 hard gate 失败时 candidate 不得进入试点。
- [ ] **真实 stable/candidate runtime**：使用独立部署和不可变 image/model/adapter digest 完成只读
  shadow、确定性 canary、冻结样本/窗口和真实自动 rollback；治理状态迁移不能代替流量验证。
- [ ] **H5 canonical 镜像**：在不依赖宿主 ROCm/venv 或运行时 Maven 下载的 registry-clean 构建中
  重建并验证 `data-alchemy:h5-canonical`，并将 pyproject 中已引入的 Presidio
  及其 spaCy 运行时依赖同步到 `uv.lock`；当前只有 local cache-backed 镜像预演证据。实施方案见
  [H5 Canonical Registry 设计](./harness/H5_CANONICAL_REGISTRY_DESIGN.md)。
- [x] **隔离测试数据重置**：`reset_pilot_environment.py` 提供 dry-run、计划 hash 确认和精确
  清理专用测试 PostgreSQL、MinIO 前缀、Redis 测试键及 Kubernetes Job；默认不得触碰共享或生产资源。
- [x] **本地 k3d 重建操作**：`LOCAL_ENVIRONMENT_OPERATIONS.md` 提供显式目标检查、集群删除/重建、
  镜像导入和 Helm 部署步骤；它与数据 reset 分开，避免误删共享数据。
- [ ] **生产 OIDC 联调**：在目标 IdP、真实 tenant/role claim 与审计留存策略下完成验收。
- [ ] **GA-01 外部试点**：两支独立真实团队连续四周使用，完成周度审计、价值回顾和安全签署。

P4 先达到 `PILOT_READY`，再进入 `GA-01`。没有外部团队时允许保持发布候选并进入
`GA-01 blocked`，但不得以内部 dogfooding、加速四周预演或 LLM 自评标记正式发布。

## 排序原则

- 先让系统可验证地完成一个复杂任务，再增加新 Connector、记忆自动化、LoRA 或多 Agent。
- 不以模拟成功、模型自评、单元测试或 Job 结束代替独立验证。
- 未具备 P0 证据包时，项目只能称为内部发布候选；单文档入库仅是检索 smoke test。
