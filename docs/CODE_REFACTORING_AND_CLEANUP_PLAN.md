# DataAlchemy 代码重构与旧 Agent 清理计划

> 目标不是减少代码行数，而是建立清晰、唯一、可验证的生产执行路径。旧 Agent 代码只有在能力已迁移、调用已归零、回归已通过后才删除。

## 1. 目标与边界

本轮重构解决四个问题：

1. `AgentRuntime` 已被定义为任务与工具执行权威，但 `rag_chat`、WebUI、CLI 和部分脚本仍依赖
   `Coordinator → AgentManager → Agent B/C/D`。
2. `Coordinator` 同时代理聊天、数据处理、训练、模型加载、反馈和资源清理，领域边界不清楚。
3. `runtime_tools.py` 通过 `Coordinator` 间接取得检索、模型、Memory、索引和流水线能力，形成隐藏依赖。
4. `webui/app.py`、`agent_runtime.py`、`runtime_tools.py` 和 `verifiers.py` 体积较大，但在权威路径收敛前直接拆文件只会搬运复杂度。

本轮不以代码行数、文件数量或类数量作为 KPI，也不做以下工作：

- 不引入第二个运行时、LangGraph、多智能体编排或通用插件框架。
- 不重写已经验证的 `AgentRuntime` 状态机、PostgreSQL RLS、MinIO evidence、审批和发布治理。
- 不因为模块名称含有 `Agent` 就删除仍被生产路径使用的能力。
- 不在同一提交中同时迁移调用、删除兼容层、拆分巨石和改变业务行为。
- 不为了统一形式把恢复演练、数据库迁移等天然独立入口强塞进一个 CLI。

## 2. 当前执行路径与问题定位

### 2.1 当前聊天路径

```text
WebUI /api/chat
  → AgentRuntime strict task
  → runtime_tools.rag_chat
  → Coordinator.chat_with_citations_async
  → AgentManager
      → AgentC.query / Retriever
      → AgentB.predict_async / BatchInferenceEngine / ModelManager
      → AgentD.fuse_and_respond
  → chat verifier / evidence
```

外层已经进入 `AgentRuntime`，但工具内部仍回落到旧 Agent 组合。这意味着运行时是任务权威，
Coordinator 仍是回答实现权威，两条架构叙述没有真正统一。

### 2.2 当前旧模块职责

| 模块 | 当前有效能力 | 主要问题 | 目标处置 |
|---|---|---|---|
| `src/agents/coordinator.py` | 聚合聊天、模型状态、流水线和反馈 | Facade 覆盖多个领域，生产路径仍调用 | 迁移调用后删除 |
| `src/core/agent_manager.py` | 懒加载 B/C/D 和 Quant 组件 | 服务定位器隐藏真实依赖 | 显式组装服务后删除 |
| `src/agents/agent_a.py` | 通过旧 Operator annotation 触发清洗 | 绕开现有 `KubernetesJobBackend` 权威路径 | 确认无调用后优先删除 |
| `src/agents/agent_b.py` | adapter 选择、加载、推理、模型状态 | 名称是 Agent，实质是模型运行服务 | 能力迁入 `inference/` 后删除包装 |
| `src/agents/agent_c.py` | PostgreSQL 检索、Memory、索引导入 | 聚合多个现有领域服务，后台同步已是空兼容 | 显式使用现有服务后删除包装 |
| `src/agents/agent_d.py` | 本地保守回答、云端证据融合 | 回答策略藏在“最终 Agent”中 | 迁入 `rag/answering.py` |
| `src/agents/agent_scheduler.py` | 定时触发旧 full-cycle | 与 Kubernetes Job/CronJob 和受控任务重复 | 替代入口确认后删除 |
| `src/core/pipeline.py` | 旧 ingest/train/full-cycle/quant 编排 | 仍调用 Agent A/C/Quant，部分路径绕过 strict task | 迁移为现有工具/Job 后删除 |
| `src/agents/quant/` | 数值特征实验 | 与当前 RAG 产品主路径弱相关 | 隔离；有 owner 和真实收益才保留 |

### 2.3 已知生产和运维调用者

重构必须覆盖以下调用者，不能只修改 `webui/app.py`：

- `webui/app.py`：Coordinator 生命周期、聊天、模型状态/重载、反馈和 Memory。
- `src/core/runtime_tools.py`：聊天、检索、索引、Memory、ingest/train/release。
- `src/run_agents.py`：ingest、train、chat、schedule、full-cycle、quant。
- `scripts/evaluate_phase1_real_tasks.py`、`scripts/core/benchmark_inference.py`：旧聊天入口。
- `scripts/core/verify_feedback.py`：旧反馈和 ingest 入口。
- `scripts/rerollout_task_bundles.py`：直接构造 `AgentC`。
- 旧 Agent 单元测试和名为 integration、实为 mock 结构测试的用例。

## 3. 目标架构

```text
WebUI / API / supported CLI
  → AgentRuntime
  → ToolRegistry
      → RAG answering
          → Retriever
          → Adapter inference
          → Grounded answer policy
      → Document / Git ingestion
      → Context and Memory
      → Kubernetes jobs
      → Evaluation and release governance
  → VerifierRegistry
  → PostgreSQL task/event/tool authority + MinIO immutable evidence
```

目标状态中：

- 生产代码不再 import `src/agents` 或 `agents`。
- `rag_chat` 直接调用明确的回答服务，不经过 Coordinator。
- 检索、Memory、模型加载、反馈、文档发布由各自领域服务拥有。
- `AgentRuntime` 仍只负责任务状态、工具调用和验证，不吸收模型或业务逻辑。
- WebUI 是组装和传输边界，不直接穿透对象内部属性取得 `agent_c.vs` 或 `agent_c.memory`。
- 工具注册按领域组织，但继续共享一个 `ToolRegistry`，不建立第二套插件系统。

## 4. 重构阶段

### R0：冻结基线与迁移清单

**目的：** 在移动实现前冻结现有可观察行为，防止“重构成功、能力丢失”。

**状态：** 已完成。迁移清单、行为矩阵与验证边界见 `docs/LEGACY_AGENT_MIGRATION_BASELINE.md`。

工作项：

1. 将旧 Agent 生产调用清单固化为 CI ratchet：允许列表只能减少，不能新增。
2. 保留 `LEGACY_AGENT_CALLS`，标签至少区分 `entrypoint` 与 `route`。
3. 为以下现有行为建立或确认回归：
   - tenant-scoped 检索与跨 tenant 拒绝；
   - 本地模式有证据回答、无证据 abstain；
   - 云端融合前执行脱敏并保存 model-call trace；
   - citations 来自实际 retrieval rows；
   - promoted adapter 选择、hash 校验、加载、状态和重载；
   - chat context 只消费一次且证据 hash 一致；
   - Memory 列表、审批、冲突和 tenant 范围；
   - side-effecting tool 审批、幂等和 reconciliation。
4. 对每个旧入口标注 owner、当前调用者、替代入口和删除条件；未知调用不得凭搜索结果直接删除。

退出门禁：

- 全仓 Ruff、现有 pytest、strict `rag_chat` 定向测试通过。
- CI 能阻止新增生产 `agents` import。
- 基线报告明确区分单元/mock、PostgreSQL 组件测试和真实模型/集群验证。

提交边界：只增加/调整测试、CI 规则和迁移清单，不移动生产实现。

### R1：提取回答与模型运行能力，不改调用路径

**目的：** 先把 Agent B/D 中的有效能力变成职责明确的普通领域代码。

**状态：** 已完成。`GroundedAnswering` 与 `AdapterRuntime` 已分别迁入 `rag/`
和 `inference/`；Agent B/D 仅保留薄兼容名称。生产路由仍经 Coordinator，留待 R2 切换。

工作项：

1. 新建 `src/rag/answering.py`，迁入：
   - 本地证据支持度判断与 abstention；
   - 云端 evidence fusion；
   - model-call trace 和云调用审计；
   - citation 组装所需的纯函数。
2. 将 `AgentB` 的职责迁到 `src/inference/`：
   - adapter 发布记录查询；
   - artifact hash 校验和精确下载；
   - `ModelManager` / `BatchInferenceEngine` 生命周期；
   - `predict_async`、`model_status`、`reload_adapter`。
3. 使用一个具体实现，不新增接口、工厂或依赖注入框架。构造函数显式接收已有服务即可。
4. 暂时让 `AgentB`、`AgentD` 成为薄兼容层并调用新实现，保证行为不变。
5. 将旧测试迁到新领域模块；兼容层只保留一条委托测试。

退出门禁：

- 新旧入口在冻结输入上产生相同 answer/citations/model status。
- 本地 abstention、云端 trace、adapter hash 失败和重载回归通过。
- 本阶段不修改 `runtime_tools.rag_chat` 的生产指向。

提交边界：回答策略和 inference 能力迁移；不删除 Coordinator，不拆 WebUI。

### R2：移除 `runtime_tools` 对 Coordinator 的依赖

**目的：** 让 `AgentRuntime → Tool Gateway` 成为真实的唯一执行路径。

**状态：** 已完成。`rag_chat`、document/RAG、Memory 工具已改为显式服务依赖；WebUI
直接组装并复用 `VectorStore`、`Retriever`、`MemoryOrchestrator`、`AdapterRuntime` 和
`GroundedAnswering`，WebSocket 与模型状态/重载不再经过 Coordinator。旧
ingest/train/evaluate/release handler 保持显式 blocked。Coordinator 尚存的反馈、CLI 和评估
兼容入口归 R3 迁移，本阶段不提前删除。

完成证据：生产改动提交 `50aa5c2`、`7968ac1`、`2ff2407`；GitHub Actions run
`33299582659` 全部通过。CI 同款 pytest 为 142 passed、38 skipped；这不代表真实 ROCm、
MinIO/Kubernetes 全链路或客户流量验证。

工作项：

1. 在 WebUI 组装边界显式创建并复用：
   - `VectorStore`；
   - `Retriever`；
   - `MemoryOrchestrator`；
   - adapter inference 具体实现；
   - RAG answering 具体实现。
2. 调整工具注册函数，使其接收所需的具体服务，不再接收整个 Coordinator。
3. `rag_chat` 直接完成：加载冻结 context → 模型推理 → grounded answer → citations → evidence recorder。
4. 文档、Git 和 RAG probe 工具直接使用 `VectorStore`/`Retriever`，不再访问
   `coordinator.agent_manager.agent_c.*`。
5. Memory 工具直接使用 `MemoryOrchestrator`，不再通过 AgentC 获取。
6. ingest/train/release 旧兼容工具继续保持 blocked，或明确转到已存在的 strict Job/发布工具；
   不允许为了保留旧 CLI 而恢复绕过路径。
7. `runtime_tools.py` 只有在依赖解除后再按领域拆分：
   - RAG/chat 工具归 `rag/`；
   - document ingestion 工具归 `etl/` 或 `connectors/`；
   - Memory 工具归 `memory/`；
   - 注册组合仍保留一个入口。

退出门禁：

- WebUI strict chat 不调用 `Coordinator.chat_*`。
- `LEGACY_AGENT_CALLS{route="runtime_adapter"}` 在回归中保持为零。
- citations、context hash、tool result、verifier 和 evidence manifest 与迁移前契约一致。
- PostgreSQL tenant/RLS、失败重试和 reconciliation 测试通过。

提交边界：先迁 `rag_chat`，再迁 document/RAG，最后迁 Memory；每类工具独立提交。

### R3：迁移 WebUI、CLI 和脚本调用者

**目的：** 清除 Coordinator 的剩余生产与运维入口。

**状态：** 已完成。WebUI 已直接管理具体服务；反馈 source/rating
写入不可变 MinIO 对象并由 PostgreSQL annotation 管理审核；默认 CLI 仅保留受控 Web/API 的
`chat` 与 `task --spec`；评估、benchmark、feedback 验证和 rerollout 脚本不再构造旧 Agent。
R4 前的部署观察已证明旧入口计数归零；结束证据见 R4 状态。

完成代码提交：`9d52332`、`edda61e`、`d836f67`、`9fc96f3`、`a098004`。GitHub Actions
run `33307076288` 全部通过；CI 同款 pytest 为 144 passed、38 skipped。实际部署中的旧入口
计数零观察期仍须由发布负责人执行，未由单元/组件测试替代。

工作项：

1. WebUI：
   - 生命周期直接管理具体服务；
   - 模型状态/重载调用 inference 服务；
   - Memory API 调用 `MemoryOrchestrator`；
   - 反馈继续走 PostgreSQL 权威索引和不可变 MinIO source，不保留 Coordinator 本地 fallback。
2. CLI：
   - `chat` 调用受控 Web/API 或复用 strict task 创建入口；
   - `ingest`、`train`、`release` 只创建受控任务，不直接调用旧 pipeline；
   - `schedule` 由部署层 CronJob/定时任务承担后删除；
   - `full-cycle` 不再作为绕过审批的快捷路径；
   - `quant` 若仍属实验，移出默认产品 CLI。
3. 脚本：
   - 评测和 benchmark 改用新回答入口；
   - feedback 验证改用权威 feedback service；
   - rerollout 改用 `Retriever`，不构造 AgentC；
   - 一次性历史脚本不为迁移而重写，确认无运行职责后归档。
4. 扩大 CI import gate 到 `src/`、`webui/` 和受支持 `scripts/`，仅测试兼容层可暂时引用旧 Agent。

退出门禁：

- 生产和受支持脚本中不再 import `agents`。
- CLI help、wheel entrypoint、WebUI chat、模型 reload、Memory 和 feedback 定向回归通过。
- 在实际部署观察窗口内，旧入口调用计数为零；观察期由发布负责人设定，不伪造固定天数。

提交边界：WebUI、CLI、脚本分别迁移，避免一个 PR 同时改变全部入口。

### R4：删除旧 Agent 与双轨代码

**目的：** 只删除已经失去运行职责的兼容层。

**状态：** 代码清理已完成。24 小时窗口从 `2026-08-30T13:56:21Z` 持续到
`2026-08-31T14:17:07Z`，同一 Pod UID、零重启，四条旧入口指标均明确存在且为零。
结束 receipt 为
`release-evidence/r4-observation/a317fc6525db010f9114b8ce2c69d6a06c3fba9e/end/sha256/c837c940b59f4141792c92e0479bfea17eb5993e24df9ccef7e79089d91c8a4f.json`，
SHA256 为 `c837c940b59f4141792c92e0479bfea17eb5993e24df9ccef7e79089d91c8a4f`；
集群中的 `r4-observation-a317fc6-end` ConfigMap 以 `immutable: true` 固化该引用。
窗口内只有健康检查流量；同一 Pod 和镜像在窗口开始前完成过一次严格聊天与引用验证，
因此这是内部重构删除证据，不升级为客户流量或 GA 声明。

删除前置条件：

1. `rg`/import gate 没有生产调用。
2. 旧入口指标在约定观察窗口内为零。
3. 新路径完成等价回归和至少一次部署环境验证。
4. 回滚依靠 Git/release artifact，而不是继续在生产包中保留旧实现。

按风险从低到高删除：

1. `AgentA` 及旧 Operator annotation 清洗路径。
2. `AgentS` 和 CLI `schedule/full-cycle` 绕过入口。
3. 已变成薄包装的 `AgentB`、`AgentC`、`AgentD`。
4. `AgentManager` 和 `Coordinator`。
5. 无剩余职责的 `PipelineManager`。
6. 对应的 mock 结构测试、旧 benchmark 和个性化旧 Agent 测试；业务断言必须已迁入新路径测试。

Quant 代码单独决策：

- 若有明确 owner、真实输入、可重复收益和产品调用者，迁出 `agents/` 并作为独立数据处理能力保留。
- 若只有实验脚本和占位增强逻辑，则删除代码、CLI 选项和文档声明。
- 不以代码行数作为保留或删除依据。

R4 决策：旧 quant 模块没有 owner、产品调用者或可重复收益证据，且只被旧
`AgentManager` / `PipelineManager` / `AgentC` 子图引用，因此随兼容层删除；依赖分组仍留给 R6。

退出门禁：

- `src/agents/`、`src/core/agent_manager.py` 不再进入生产 wheel；满足全部迁移条件时可整体删除。
- 全仓不存在 Coordinator/AgentManager 生产引用。
- 全量测试、构建、Helm、strict task 与发布证据回归通过。

提交边界：删除兼容层单独提交，便于审查和必要时整体回退。

### R5：拆分仍然存活的巨石模块

**目的：** 在删除历史路径后改善剩余代码的局部可理解性，而不是搬运旧复杂度。

优先级：

1. `webui/app.py`
   - 按 API 责任拆成 chat/tasks、data/release、memory/feedback 三组 router；
   - 认证、identity 和服务实例继续共享现有实现；
   - router 只做请求校验、调用和 HTTP 错误映射，不复制业务逻辑。
2. `core/verifiers.py`
   - 保留一个 `VerifierSpec`/registry 入口；
   - 按 task/evidence、RAG/ingest、memory、evaluation/release 分模块；
   - verifier 函数保持纯只读语义，不新增 verifier 框架。
3. `core/agent_runtime.py`
   - 状态机、lease、审批、执行循环继续集中；
   - 只把稳定且独立的 `ToolSpec`/`ToolRegistry` 契约移出；
   - 不引入 repository/service/factory 多层包装。
4. `core/runtime_tools.py`
   - R2 已按领域解除依赖后，仅保留工具注册组合；
   - 不再集中保存所有领域 handler。

每次拆分必须满足：

- 业务行为和数据库 schema 不变；
- 一个 PR 只拆一个责任域；
- 原模块兼容 import 不长期保留；
- 对拆出的公共函数有实际第二个调用者，否则保持私有；
- 对已收敛文件逐步取消 Ruff `C901` 豁免。

### R6：脚本、依赖与文档卫生

**目的：** 让支持范围清楚，而不是追求只有一个入口。

工作项：

1. 将脚本标记为 supported、release evidence、development、archived 四类。
2. supported 脚本只做参数解析并调用包内实现；业务逻辑不得只存在于脚本中。
3. release 决策与恢复演练保留独立入口和证据归档，不等待“操作事故后再统一”。
4. 历史 phase 脚本只有在 CI、文档、运维和 evidence 重放都不再引用后才归档。
5. 依赖按 web、training、etl、dev 分组；WebUI 镜像不安装训练/Spark 栈。
6. 删除代码和构建流程都没有引用的直接依赖；间接依赖或运行时插件不能只凭静态搜索删除。
7. `ARCHITECTURE.md` 只描述稳态架构；迁移状态和删除观察结果记录在本计划或
   `RELEASE_STATUS.md`，不要把临时分支信息写回架构文档。

## 5. 回归与验证矩阵

| 风险面 | 最小回归 | 环境级验证 |
|---|---|---|
| RAG 回答 | 有证据回答、无证据 abstain、引用字段、prompt injection 拒绝 | PostgreSQL pgvector/FTS + 真实 context capture |
| 模型运行 | base/adapter 选择、hash、reload、model status、失败 trace | 目标 ROCm 模型加载和一次固定问答 |
| AgentRuntime | 审批、幂等、暂停/恢复、deadline、reconciliation | PostgreSQL service CI |
| 文档工具 | input hash、rough/refine/publish、检索 verifier | MinIO + PostgreSQL + 隔离 Kubernetes Job |
| Memory | tenant、审批、冲突、过期、删除 | PostgreSQL RLS |
| Feedback | 不可变 source、权威索引、review/training policy | PostgreSQL + MinIO |
| 发布 | golden receipt/manifest、shadow/canary/rollback decision | 隔离 candidate runtime；治理状态不能代替流量验证 |
| 旧路径清理 | import gate、legacy metric、wheel contents | 部署观察窗口零调用 |

每个重构 PR 至少执行：

```bash
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/pytest -q
git diff --check
```

涉及 packaging、Helm 或 Job 的 PR 还必须执行相应 wheel、Helm template/lint 和隔离环境回归。
不得用 mock 单测通过替代 PostgreSQL、MinIO、Kubernetes 或真实模型门禁。

## 6. 交付顺序与提交策略

建议按以下独立变更集交付：

1. `test: freeze legacy agent behavior and import ratchet`
2. `refactor: move grounded answering out of AgentD`
3. `refactor: move adapter runtime out of AgentB`
4. `refactor: route rag chat directly through runtime services`
5. `refactor: remove AgentC service locator dependencies`
6. `refactor: migrate webui model memory and feedback services`
7. `refactor: migrate supported cli and evaluation callers`
8. `chore: remove retired agent compatibility path`
9. 后续按领域拆 WebUI、verifiers 和 runtime tools；不与旧 Agent 删除混在同一提交。

每个提交都应可构建、可测试、可独立回退。禁止建立长期双写或用 feature flag 永久保留两套回答路径；
需要短期对比时只做 shadow 观测，不让两个实现同时产生业务副作用。

## 7. 完成定义

本计划完成时应满足：

- `AgentRuntime + ToolRegistry + VerifierRegistry` 是唯一任务执行与完成判定路径。
- WebUI、CLI 和受支持脚本不再调用 Coordinator 或 Agent A--D。
- RAG、模型、Memory、ingestion、feedback 和 release 各自有清晰 owner 与模块边界。
- 旧 Agent 代码的删除由调用归零和等价验证证明，不由行数目标推动。
- 剩余巨石按稳定责任拆分，CI 复杂度豁免持续减少。
- 真实客户/内部试点仍未验证的能力继续明确标记为未验证，不因重构完成而升级产品声明。
