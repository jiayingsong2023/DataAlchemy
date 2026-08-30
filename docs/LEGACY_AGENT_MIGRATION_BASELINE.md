# Legacy Agent 迁移基线

> R0 冻结日期：2026-08-30。基线父提交：`17b2756`。本文件记录迁移边界，不构成生产能力声明。

## 1. 基线目的

在迁移 `src/agents` 前固定当前可观察行为、生产 import 清单和删除条件。R0 只调整测试、CI
ratchet 和迁移记录，不移动业务实现，不删除兼容入口。

CI 对 `src/`、`webui/`、`scripts/` 中所有静态 `agents` / `src.agents` import 做精确清单比较。
清单可以随迁移减少；新增或改变 import 必须使 CI 失败并接受架构复核。

## 2. 当前生产 import 清单

| 调用者 | 当前依赖 | 所有者 | 目标替代 | 删除条件 |
|---|---|---|---|---|
| `webui/app.py` | `Coordinator` | Web/API | 显式组装 RAG、inference、Memory、feedback 服务 | WebUI 回归不再构造 Coordinator |
| `src/core/agent_manager.py` | Agent A/C、Quant agents | Runtime assembly | 各领域具体服务 | 所有生产调用者迁移且指标观察期为零 |
| `src/run_agents.py` | `Coordinator`、`AgentS` | CLI/Operations | supported CLI 调用 runtime/tool/job | 对应 CLI 行为有替代入口和回归 |
| `src/rag/quant_enhancer.py` | Quant utils | RAG experiments | 隔离实验实现或删除 | 有 owner 和收益证据，否则无生产调用后删除 |
| `scripts/evaluate_phase1_real_tasks.py` | `Coordinator` | Evaluation | strict `rag_chat` | 新旧固定输入等价且 evidence 可重放 |
| `scripts/core/benchmark_inference.py` | `Coordinator` | Inference | inference 具体服务 | benchmark 不再依赖回答 facade |
| `scripts/core/verify_feedback.py` | `Coordinator` | Feedback | feedback/ingestion 具体服务 | feedback 与 ingest 回归通过 |
| `scripts/rerollout_task_bundles.py` | `AgentC` | Experience rollout | `Retriever` / `VectorStore` | rerollout scope/citation 回归通过 |

当前精确 import 文本由 `.github/workflows/ci.yml` 的 `Block new legacy Agent imports` 保存；该处是
机器门禁，本表是职责和处置依据。

## 3. 旧入口处置清单

| 旧入口 | 当前调用者 | 目标入口 | 删除证据 |
|---|---|---|---|
| `Coordinator.chat_async` | runtime fallback、真实任务评估、benchmark | `AgentRuntime → rag_chat → answering service` | `entrypoint/route` 指标连续观察为零；回答基线通过 |
| `Coordinator.chat_with_citations_async` | strict `rag_chat` adapter | answering service | `runtime_adapter` 指标为零；citation/context verifier 通过 |
| Coordinator ingest/train/full-cycle/quant | WebUI、CLI、feedback 脚本 | strict tools、Kubernetes Jobs、release workflow | 审批、幂等、job reconciliation 和恢复回归通过 |
| `AgentA` | `PipelineManager` | `KubernetesJobBackend` 文档任务 | 旧 pipeline 无调用且隔离 Job 回归通过 |
| `AgentB` | Coordinator、model status/reload | `inference/` adapter runtime | promoted release、hash、tenant、load/reload/status 等价 |
| `AgentC` | Coordinator、ingest、Memory、rerollout | `Retriever`、`VectorStore`、`MemoryOrchestrator` | tenant/RLS、索引、Memory 和 rerollout 回归通过 |
| `AgentD` | Coordinator | `rag/answering.py` | local abstention、cloud sanitization/trace、citation 等价 |
| `AgentS` | legacy CLI schedule | 平台 CronJob 或受控任务 | 运维入口和 owner 明确，旧 schedule 无调用 |
| Quant agents | AgentManager、旧 quant CLI | 隔离实验路径 | 明确 owner 和收益证据；否则无调用后删除 |

不得仅凭静态搜索删除未知入口。删除前必须同时满足：import ratchet 已减少、对应
`LEGACY_AGENT_CALLS` 序列为零、替代路径回归通过、部署产物不再包含该调用。

## 4. 行为冻结矩阵

| 风险面 | 回归证据 | 证据级别 | R0 结论 |
|---|---|---|---|
| tenant-scoped 文档检索及跨 tenant 拒绝 | `tests/test_memory_orchestrator.py` | PostgreSQL 组件 | 已覆盖 |
| 本地有证据回答、无证据 abstain、prompt injection | `tests/test_agent_d_local_grounding.py` | 单元 | 已覆盖 |
| 云端调用前脱敏及 model-call trace | `tests/test_execution_mode.py` | mock 单元 | R0 新增；未验证真实云服务 |
| citations 来自传入 retrieval rows | `tests/test_execution_mode.py`、`tests/test_verifiers.py` | 单元/verifier | R0 补强 |
| promoted adapter 选择、tenant、hash、加载和状态 | `tests/test_agent_b_output.py` | mock/文件单元 | R0 新增；未加载真实 ROCm 模型 |
| chat context 只加载一次并绑定 hash | `tests/test_runtime_tools.py`、`tests/test_verifiers.py` | 单元/verifier | 已覆盖 |
| Memory 列表、审批、冲突、tenant 范围 | `tests/test_memory_orchestrator.py`、`tests/test_h4_context_memory.py` | PostgreSQL 组件 | 已覆盖 |
| side-effecting tool 审批、幂等、reconciliation | `tests/test_agent_runtime.py`、`tests/test_jobs.py` | PostgreSQL 组件 | 已覆盖 |
| WebUI strict chat 的 run/session/evidence 绑定 | `tests/test_el1_chat.py` | mock 单元 | 已覆盖；不等于 HTTP E2E |
| legacy 入口及 route 指标 | `tests/test_execution_mode.py` | 单元 | R0 补强两个聊天入口 |

证据级别含义：

- “单元/mock”只证明确定性逻辑和调用契约。
- “PostgreSQL 组件”在 CI 的真实 pgvector PostgreSQL 和 RLS 迁移后运行，但不包含 MinIO、GPU 或 Kubernetes。
- 真实模型、MinIO/Kubernetes 环境和客户流量必须单独验证，不能由上述回归替代。

## 5. 冻结时验证状态

R0 父基线 `main@17b2756` 的 GitHub Actions run `33289008732`：

- Ruff 全仓通过，179 个文件格式检查通过；
- pytest：170 passed、3 skipped、6 warnings；`tests/test_integration.py` 明确未执行；
- Phase 1--4 现有评估脚本、Helm lint/template 和 production privileged 拒绝门禁通过；
- 结果证明当前工程基线可重放，不证明真实 ROCm 模型、MinIO/Kubernetes 全链路或客户验收。

R0 完成后的提交必须重新执行同一套 hosted CI；只有门禁通过，才进入 R1。

R0 提交 `ad2810a` 已由 draft PR #20 的 GitHub Actions run `33289612481` 重放：

- pytest：175 passed、3 skipped、6 warnings；
- Ruff、legacy import ratchet、Phase 1--4 评估、Helm 与 production 安全门禁全部通过。

R0 退出门禁已满足；未验证的真实 ROCm 模型、MinIO/Kubernetes 全链路和客户流量仍保持未验证标记。
