# DataAlchemy 当前发布状态

> 本文件是当前阶段状态的事实来源；各阶段
> 退出报告保留其验收时的历史上下文，不因历史报告中的分支名或镜像标签变化而自动更新。
> 隔离恢复由季度 workflow 重放并归档 120 天证据；证据过期时不得继续声明恢复门禁有效。

| 阶段 | 状态 | 核心交付 | 当前证据 |
| --- | --- | --- | --- |
| Phase 0 安全基线 | 已完成 | tenant 贯通 JWT/会话/缓存、反馈审核门禁、纯本地/云增强边界、流水线故障传播、CI 与评测基线 | 后续 Phase 1--4 回归仍覆盖认证、配置、租户隔离与失败路径 |
| Phase 1 单智能体运行时 | 已完成 | 单一 `Plan → Act → Observe → Replan` 运行时、任务事件、审批、暂停/恢复、重试、幂等工具与 WebUI 任务面板 | Phase 1 基线 5/5；运行时回归覆盖审批、重试、限流、脱敏与租户隔离 |
| Phase 2 分层记忆 | 已完成 | PostgreSQL + pgvector/FTS/RRF 作为检索与记忆权威、RLS、文档/记忆 ACL、候选审批、更正、删除及 Redis 收缩 | 记忆评测 20/20 Recall@1；未审批与跨 tenant 召回为 0；隔离恢复通过 |
| Phase 3 工具化试点 | 发布候选完成 | Git 文件正文与 ACL 同步、删除/版本替换、受控工具网关、运行 manifest、控制台、恢复脚本与双 tenant 预演 | 四周压缩预演：80/80 任务、8/8 审批/恢复、跨 tenant 可见性为 0 |
| Phase 4 企业治理 | 发布候选完成 | OIDC + PKCE、审计事件、记忆到期策略回放、SLO 汇总、受控发布与自动回滚、内部 Alpha、GA-01 包 | 41 项测试；两次发布周期（一晋级、一自动回滚）；`phase4_restore` 隔离恢复通过 |
| H5 Harness 学习与发布 | 工程预演完成，canonical 镜像门禁未闭合 | 轨迹评测、训练快照、GPU LoRA、adapter 评测、shadow/canary、回滚与发布 API | 本地 cache-backed 镜像上的真实 k3d/GPU 预演通过；不能替代 registry-clean 构建 |
| H6 PILOT_READY / GA | 模拟预演通过，外部门禁未关闭 | 真实数据资格、独立人工校准、stable/candidate、reset/restore、试点证据与 OIDC/RLS 边界 | synthetic `PILOT_READY` 7/7；真实代表性数据和 `GA-01` 两团队四周试点尚未开始 |
| TVE / Experience Learning | synthetic engineering GO / promoted | v3 Task Bundle 150/44/100、三环境 reset/preflight、独立 verifier、Experience Compiler、GPU LoRA、三次冻结 holdout A/B、tiered decision、shadow/offline canary | adapter 三次均 98/100，base 38/37/37，critical 100%，decision `18713148…dab9` 为 GO；release `5c974571…08fb` 已 promoted |

## 本轮工作摘要

1. 冻结 v3 Task/Environment/Verifier：发布 150 train、44 validation、100 holdout Task Bundle，
   环境 reset/preflight 与 verifier input 分离保存。
2. 修复评测污染与召回：模型侧不再按隐藏 `required_pages` 重排，retriever 扩大 source-scoped recall pool；
   `Document scope` 过滤只读取 Task query 和检索结果，同一规则作用于 base/candidate。
3. 完成 Experience 学习：DeepSeek 双遍审核只覆盖 train/validation，compiler 支持 reviewed-success retention、
   scope-ranked transform、旧 manifest 复用与 holdout 排除；多个训练候选均经过 validation 后再决定是否进入 holdout。
4. 完成发布证据：`evaluate_repeated_release.py` 聚合三次不可变报告，`verify_release_decision@1` 独立重放
   report、300 条 candidate transcript、critical、准确率、improvement 与 p95。
5. 完成工程晋级：decision 与精确 adapter digest 绑定，`promote_tiered_release.py` 执行
   candidate → shadow → offline canary → promoted，并保留 base rollback。
6. 修复运行问题：Kubernetes Job 的 code/model host mount 已解耦；失败的临时 Job 已清理。

主要复现入口：`scripts/import_multidoc2dial_fixture.py`、`scripts/publish_rag_suite.py`、
`scripts/rerollout_task_bundles.py`、`scripts/review_gap_with_deepseek.py`、
`scripts/compile_sft_experiences.py`、`scripts/evaluate_repeated_release.py` 和
`scripts/promote_tiered_release.py`。运行数据保留在 PostgreSQL/MinIO，不进入源码提交。

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

公共 MultiDoc2Dial v3 release suite 已发布 train 150、validation 44、holdout 100，共 294 个 Task Bundle；三个
独立环境均完成真实 reset/preflight；TinyLlama 完成 validation 和三次 holdout A/B，Qwen2.5
完成 validation 诊断后按停止门禁未进入 holdout。DeepSeek V4 双 pass 仅审核 train/validation gap，
全部标记 `human_reviewed=false`，holdout 未用于训练。

v2 的 89/100、87/100 与原 100% policy 的 NO-GO 保留为历史证据，没有被追溯改写。v3 先在
validation 验证 source-scoped `Document scope` 精确过滤（base 20/44、adapter 44/44），再对同一冻结
holdout 完整重跑三次。过滤只读取 Task query 与检索结果，不读取隐藏 verifier 条件；找不到匹配时保留
原 top-5，因此两个检索缺口仍真实失败。

| 模型 | Base | Candidate | 回归 | EL-3 |
| --- | ---: | ---: | --- | --- |
| TinyLlama + adapter `55365867…f5b5` | 38/37/37 | 98/98/98 | 三次 100-case holdout，0 invalid | `GO / tiered_policy_passed` |
| Qwen2.5-0.5B-Instruct | validation 4/44 | 无 adapter | 明显低于 TinyLlama candidate，未进入 holdout | `NO-GO / not selected` |

`verify_release_decision@1` 从三份 gap report、300 条 candidate transcript、fingerprint 和延迟重新计算
critical、普通能力、improvement 与 p95；decision `18713148…dab9` 为 GO。adapter 已 verified，engineering
release `5c974571…08fb` 经 shadow 与 300-sample offline canary 后 promoted。EL-4 DPO 与 EL-5 RL 仍为
`NOT-ENABLED`：SFT 已达当前 synthetic policy，没有为增加算法复杂度而继续训练；Agent Lightning 为
`NOT-SELECTED`。

## 当前发布结论

项目已达到**synthetic engineering GO**：公共 v3 Agent Learning 候选已在本地治理状态机晋级，工程、
双 tenant 预演、H5 GPU 工程预演与 H6 模拟资格链路已验证。它尚未达到正式生产发布：当前 canary 是
离线 synthetic holdout，不是线上流量；DeepSeek synthetic 审核不能替代人工校准，H5 canonical 镜像仍需 registry-clean
构建，OIDC 提供商需在目标部署环境联调，且 `GA-01` 要求两支独立真实团队
连续四周使用、周度审计并签署任务价值和安全结果。内部 Alpha、模拟预演和本地测试都不
能替代该门禁。

相关证据：[Phase 2 退出报告](./archive/phases/PHASE2_EXIT_REPORT.md)、
[Phase 3 退出报告](./archive/phases/PHASE3_EXIT_REPORT.md)、
[Phase 4 发布候选报告](./release/PHASE4_RELEASE_CANDIDATE_REPORT.md)、
[Agent Learning 实施计划](./harness/EXPERIENCE_FIRST_AGENT_LEARNING_PLAN.md)、
[GA-01 试点包](./release/GA01_PILOT_PACK.md)。
