# DataAlchemy 当前发布状态

> 当前分支：`feat/harness-tve`；Agent Learning 功能证据基线：`7d312ce`（2026-08-25 检查）。
> 本文件是当前阶段状态的事实来源；各阶段
> 退出报告保留其验收时的历史上下文，不因历史报告中的分支名或镜像标签变化而自动更新。

| 阶段 | 状态 | 核心交付 | 当前证据 |
| --- | --- | --- | --- |
| Phase 0 安全基线 | 已完成 | tenant 贯通 JWT/会话/缓存、反馈审核门禁、纯本地/云增强边界、流水线故障传播、CI 与评测基线 | 后续 Phase 1--4 回归仍覆盖认证、配置、租户隔离与失败路径 |
| Phase 1 单智能体运行时 | 已完成 | 单一 `Plan → Act → Observe → Replan` 运行时、任务事件、审批、暂停/恢复、重试、幂等工具与 WebUI 任务面板 | Phase 1 基线 5/5；运行时回归覆盖审批、重试、限流、脱敏与租户隔离 |
| Phase 2 分层记忆 | 已完成 | PostgreSQL + pgvector/FTS/RRF 作为检索与记忆权威、RLS、文档/记忆 ACL、候选审批、更正、删除及 Redis 收缩 | 记忆评测 20/20 Recall@1；未审批与跨 tenant 召回为 0；隔离恢复通过 |
| Phase 3 工具化试点 | 发布候选完成 | Git 文件正文与 ACL 同步、删除/版本替换、受控工具网关、运行 manifest、控制台、恢复脚本与双 tenant 预演 | 四周压缩预演：80/80 任务、8/8 审批/恢复、跨 tenant 可见性为 0 |
| Phase 4 企业治理 | 发布候选完成 | OIDC + PKCE、审计事件、记忆到期策略回放、SLO 汇总、受控发布与自动回滚、内部 Alpha、GA-01 包 | 41 项测试；两次发布周期（一晋级、一自动回滚）；`phase4_restore` 隔离恢复通过 |
| H5 Harness 学习与发布 | 工程预演完成，canonical 镜像门禁未闭合 | 轨迹评测、训练快照、GPU LoRA、adapter 评测、shadow/canary、回滚与发布 API | 本地 cache-backed 镜像上的真实 k3d/GPU 预演通过；不能替代 registry-clean 构建 |
| H6 PILOT_READY / GA | 模拟预演通过，外部门禁未关闭 | 真实数据资格、独立人工校准、stable/candidate、reset/restore、试点证据与 OIDC/RLS 边界 | synthetic `PILOT_READY` 7/7；真实代表性数据和 `GA-01` 两团队四周试点尚未开始 |
| TVE / Experience Learning | 公共 synthetic 全链完成，发布门禁阻塞 | Task Bundle、reset/preflight、独立 verifier、双模型 rollout、Experience、SFT compiler/GPU 训练、base/adapter A/B、DPO/RL 条件决策 | 两个 adapter 保持 `candidate`；EL-3 均为 `BLOCKED / training_cost_missing`，DPO/RL 未启用 |

## 关键架构收敛

- **任务与事件**：`AgentRuntime` 使用 PostgreSQL 持久化任务、事件、审批、工具幂等与
  tenant RLS。Phase 1 的 SQLite 实现是历史阶段交付，已在 Phase 2 被 PostgreSQL 权威
  路径替代。
- **检索与记忆**：文档检索为 PostgreSQL pgvector + PostgreSQL FTS + RRF，CrossEncoder
  负责精排；FAISS、BM25 文件索引和 SQLite RAG 元数据不再是当前权威路径。
- **缓存与对象存储**：Redis 仅用于有 tenant scope 与 TTL 的短期状态；MinIO 保存原始
  不可变对象及运行产物，`runs/{run_id}/manifest.json` 经哈希验证后原子更新 `current`。
- **治理与发布**：生产环境要求 OIDC；审计记录脱敏并受 RLS 保护。发布候选需含评测、
  回滚目标与 guardrail，灰度异常自动回滚。LoRA 默认关闭，当前部署仍受
  `single_tenant_lora` 边界约束。
- **学习资产**：Task Bundle、Environment receipt 与独立 Verifier 是可跨模型重放的上游资产；
  Experience、compiled snapshot 和 adapter 依次派生。PostgreSQL 保存治理投影，MinIO 保存内容寻址证据，
  Kubernetes Job 不成为训练状态权威。

## 当前可运行的产品闭环

代码已提供受控 PDF/DOCX 单文件入口 `POST /api/pilot-runs/document`：

```text
WebUI 上传 → MinIO raw/harness → strict AgentRuntime
→ Spark rough clean → deterministic refine → PostgreSQL documents/chunks
→ RAG probe / WebUI 问答 → session memory distillation
```

同一用户闭环还可将 WebUI 反馈按 `run_id` 写入 PostgreSQL annotation 权威索引，
审核后由 `scripts/run_pdf_full_cycle.py --stage h5` 继续执行训练快照、GPU LoRA、
固定评测、发布预演与 WebUI model reload。它是一个可恢复的两阶段受治理入口，
不是“上传 PDF 后无审批自动发布 adapter”。

问答路径以 RAG 引用为根据：无云模型时直接输出证据回答或在证据不足时
拒答；云增强模式可将 RAG context 与已加载 adapter 的 intuition 交给 DeepSeek 融合，
但外发前必须通过 Presidio 脱敏门禁并写入 cloud audit；Presidio 不可用时 fail closed。

## Agent Learning 当前门禁

公共 MultiDoc2Dial fixture 已发布 train 20、validation 8、holdout 12，共 40 个 Task Bundle；TinyLlama
与 Qwen2.5 完成 base rollout。DeepSeek V4 对 25 个唯一 weak/failed train/validation task 做双遍审核，
发布 40 个按模型授权的 synthetic label，但全部为 `human_reviewed=false`，holdout 未用于训练。

TinyLlama snapshot 含 12 train/3 validation，Qwen2.5 snapshot 含 17 train/8 validation；两个真实 GPU
Job 均训练 50 steps，adapter 通过 safetensors 扫描并保持 `candidate`。受控 A/B 结果为：

| 模型 | Base | Candidate | 回归 | EL-3 |
| --- | ---: | ---: | --- | --- |
| TinyLlama | 20/40 | 23/40 | validation 5/8→4/8；holdout 7/12→5/12 | `BLOCKED / training_cost_missing` |
| Qwen2.5-0.5B-Instruct | 5/40 | 5/40 | holdout 2/12→0/12 | `BLOCKED / training_cost_missing` |

两组报告均由独立 verifier 从 transcript、adapter artifact、snapshot 与 compile manifest 重建。已观测
Job wall time 尚未形成不可变训练成本证据，且质量结果不支持发布。因此 EL-4 DPO 为
`NOT-ENABLED / sft_not_validated`，EL-5 RL 为 `NOT-ENABLED`，Agent Lightning 为 `NOT-SELECTED`。

## 当前发布结论

项目已达到**内部发布候选**：工程、双 tenant 预演、受控发布、H5 GPU 工程预演与 H6
模拟资格链路已验证，公共 synthetic Agent Learning 流程也已执行到正确的阻塞终态。它尚未达到正式生产
发布：候选 adapter 未晋级，DeepSeek synthetic 审核不能替代人工校准，H5 canonical 镜像仍需 registry-clean
构建，OIDC 提供商需在目标部署环境联调，且 `GA-01` 要求两支独立真实团队
连续四周使用、周度审计并签署任务价值和安全结果。内部 Alpha、模拟预演和本地测试都不
能替代该门禁。

相关证据：[Phase 2 退出报告](./archive/phases/PHASE2_EXIT_REPORT.md)、
[Phase 3 退出报告](./archive/phases/PHASE3_EXIT_REPORT.md)、
[Phase 4 发布候选报告](./release/PHASE4_RELEASE_CANDIDATE_REPORT.md)、
[Agent Learning 实施计划](./harness/EXPERIENCE_FIRST_AGENT_LEARNING_PLAN.md)、
[GA-01 试点包](./release/GA01_PILOT_PACK.md)。
