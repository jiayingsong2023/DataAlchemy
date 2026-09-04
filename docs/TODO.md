# DataAlchemy Agent Harness 待办清单

> 目标不是堆叠 Agent 功能，而是让模型在明确上下文、受控工具和真实环境中完成复杂任务，
> 并用独立证据证明完成、失败或需要人工决策。当前不引入第二个运行时；以 PostgreSQL
> `AgentRuntime`、Tool Gateway、MinIO 产物和发布治理为唯一权威路径。

> **状态复核：2026-09-03；运行证据以内容哈希为准，分支名不作为能力状态依据。**
> `[x]` 表示当前代码、测试或真实工程
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
- [x] **TVE 模型无关资产闭环**：v2 已发布 train 200、validation 78、holdout 100，共 378 个
  Task Bundle；三套环境均有真实 reset/preflight receipt，并由 TinyLlama/Qwen2.5 在相同 suite 上
  re-rollout；环境失败与模型失败分开统计。
- [x] **Experience Compiler 与 gap-only SFT 工程闭环**：受治理 Experience、label、split、target
  tokenizer/template、compiled JSONL 与 manifest 可反向追溯；holdout、solved、revoked 与错误恢复路径
  不进入训练。两轮 TinyLlama snapshot 均以 completion-only loss 在真实 GPU Job 训练，并保存成本回执。
- [x] **受控 base/adapter A/B 与停止门禁**：v2 NO-GO 历史保留；v3 使用冻结 100-case holdout
  完整重跑三次，base 38/37/37、adapter 98/98/98、0 invalid。独立 verifier 重算 decision 为 GO，
  adapter 已 verified，engineering release 已 promoted。
- [x] **DPO/RL 条件决策**：当前 SFT 已通过 synthetic policy，没有新增 DPO/RL 的必要收益假设，
  因此仍保持 `NOT-ENABLED`，Agent Lightning 保持 `NOT-SELECTED`；未创建第二套 store/controller。

## P4：试点运维与正式发布

### RTD 后续资格门禁

按 [RAG 与后训练数据边界设计](./RAG_AND_TRAINING_DATA_BOUNDARY_DESIGN.md#12-rtd-后续资格门禁)
中的 `RTD-Q0 → RTD-Q5` 顺序执行；以下是聚合门禁，详细实施项仍以本节后续既有待办为准。

- [x] **RTD-Q0 资格契约冻结**：产品声明、内部受控数据、suite、阈值、SLO、责任主体和变更规则已冻结。
  - [x] `qualification_manifest.v1` 严格校验和独立 verifier 已实现；frozen manifest SHA-256 为
    `fa2c46bb...94852b`，blocker 为空；
  - [x] source manifest（`dec61d0f...0e227`）与扩展 suite（`b121f2ae...f9005`）已绑定，四个工程治理
    责任主体已分离，内部性能基线已冻结；真实数据、真人复核与业务签署仍由 RTD-Q5 关闭。
- [x] **RTD-Q1 受治理 compiler 重放**：干净构建的 H5 镜像在两个全新 Job 中重复编译并由独立
  verifier 重放；dataset、manifest、tokenizer/template、completion mask 均确定一致。内容寻址
  receipt SHA-256 为 `6e3a041f...15e1b`；本地 k3d 导入不替代尚未关闭的 H5 canonical GHCR 门禁。
- [x] **RTD-Q2 扩展联合资格评测**：七类冻结 case、16 个质量/安全/工具 gate 与内部性能下限均通过；
  严格工具任务覆盖冲突、拒绝审批和批准执行，冷缓存逐 case RTD4 receipt 为 `4f53a22e...3c6d0`，
  最终聚合 decision receipt 为 `8891d8e6...b9d86`。该结论仍是 synthetic engineering
  qualification，目标负载与真人试点分别由 RTD-Q4/RTD-Q5 关闭。
- [x] **RTD-Q3 撤销后干净重建**：隔离 tenant 已完成旧 snapshot/adapter 撤销、旧 release 回滚、
  clean snapshot 确定性重编译、replacement adapter 训练验证、联合评测、注入失败回滚及重新晋级；
  clean-rebuild receipt 为 `fb562989...8fbd`。结论仅覆盖 synthetic engineering 与
  RAG-authoritative 联合路径，不声明 standalone adapter 业务增益。
- [ ] **RTD-Q4 目标负载性能资格**：在代表性规模和并发下处理 RTD1 的 1.559 倍延迟观察，质量与
  延迟/容量 SLO 必须同时达标。
- [ ] **RTD-Q5 真实试点与 GA-01**：关闭真实数据、人工校准、stable/candidate runtime、OIDC 和两团队
  四周试点；缺少外部条件时标记 `GA-01 blocked`。

- [ ] **真实数据资格认证**：为授权、脱敏且代表目标任务分布的数据建立不可变 manifest，记录
  owner、tenant、ACL、用途、许可、保留/删除策略和 suite 隔离；synthetic 数据只能用于工程回归。
- [ ] **人工校准与候选资格**：复用 H5 annotation/evaluation，以独立 reviewer 校准 LLM judge，
  对失败轨迹和安全 case 做人工复核；缺少校准或 hard gate 失败时 candidate 不得进入试点。
  DeepSeek V4 已完成公共 synthetic fixture 双遍初审，但 v2 标签均为
  `human_reviewed=false`，不能关闭本项。
- [x] **不可变训练成本证据**：将训练 token、steps、wall time、GPU/镜像 digest、预算与计量口径写入
  内容寻址 `training_cost_receipt.v1` 并由独立 verifier 复核；能力门禁仍单独阻塞发布。
- [x] **无回归的 synthetic 候选质量**：TinyLlama adapter 三次 holdout 均为 98/100，base 平均
  37.33%，critical 100%，p95 门禁通过；该结论只关闭公共 synthetic engineering gate。
- [x] **失败归因与分层 policy**：已实现并实测 `release_policy.v1`：critical 100%、普通能力最低
  90%、相对 base 提升至少 1 个百分点、p95 比率不超过 1.20、至少三次 rollout；
  `verify_release_decision@1` 可从不可变报告重放 GO。
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
- [x] **Web TinyLlama GPU 回归恢复**：本地 Helm GPU 分支显式挂载节点 ROCm userspace；最小
  FP16 GEMM 与真实 chat → feedback 已重放，未用 CPU 降级替代 GPU 验证。
- [x] **RTD1 RAG 投影受控 A/B**：同一 source version、模型和 7 个冻结问题下，旧/新投影的
  Recall@5 与 context coverage 均为 1.0，MRR 均为 0.928571；新投影 citation precision
  从 0.20 提升到 0.257143。内容寻址 report 为 `e2be7011...02c307`。CPU reranker 延迟增加
  至 1.559 倍，作为扩大语料或调整 chunk policy 前必须复跑的非阻塞性能观察项。
- [x] **RTD2 reviewed-feedback compiler 桥**：
  - [x] reviewer correction 重发匹配的新不可变 annotation content，并保留原评分对象引用；
  - [x] approved feedback 已投影为原有 Task/Experience 契约；真实双模型 rerollout 生成有效 gap，
    独立 reviewer 批准两条 Experience 后，compiler 创建 1 train + 1 validation 的 candidate snapshot
    `3e8c76fe-1b11-44a4-a989-78330c6c8d45` 并通过 manifest/hash 验证。
- [x] **RTD3 撤销与权限传播**：隔离 tenant 中完成 RAG ACL/source 撤销，以及 source version、
  ACL digest、permission version 三条 annotation → snapshot → adapter → release 影响链回滚；
  新 adapter 与 release 重新晋级均被拒绝，split contamination 为 0。内容寻址 receipt 为
  `fbf46200...84335f`，只关闭工程门禁，不替代真实业务授权。
- [x] **RTD4 联合门禁与旧入口删除**：删除 `build_pdf_training_candidates` 直接训练入口并加 CI
  ratchet；精确 GPU 镜像 `sha256:d1548b4c...1923e6` 上 base+RAG 与 promoted-adapter+RAG 均
  通过 7/7 文本、页码和 citation lineage。关闭 receipt 为 `e33a152f...ab03e6`；local RAG
  权威策略下两臂联合效应为 neutral，不作为 adapter 业务增益声明。
- [ ] **GA-01 外部试点**：两支独立真实团队连续四周使用，完成周度审计、价值回顾和安全签署。
- [ ] **Ray Data 条件评估（不阻塞 GA-01）**：只有真实训练数据生产出现 CPU/GPU 混合批处理，
  且普通 Python 基线不能满足目标时才启动；按 [Ray Data 候选评估路线图](./RAY_DATA_EVALUATION_ROADMAP.md)
  完成基线恢复、最小 PoC、ROCm 验证和受控 A/B。得到不可变 `GO` receipt 前不添加当前能力声明，
  也不长期并存 Spark 与 Ray 两套分布式执行栈。

P4 先达到 `PILOT_READY`，再进入 `GA-01`。没有外部团队时允许保持发布候选并进入
`GA-01 blocked`，但不得以内部 dogfooding、加速四周预演或 LLM 自评标记正式发布。

## 排序原则

- 先让系统可验证地完成一个复杂任务，再增加新 Connector、记忆自动化、LoRA 或多 Agent。
- 不以模拟成功、模型自评、单元测试或 Job 结束代替独立验证。
- 未具备 P0 证据包时，项目只能称为内部发布候选；单文档入库仅是检索 smoke test。
