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
| H5 Harness 学习与发布 | canonical 工程门禁已闭合 | 轨迹评测、训练快照、GPU LoRA、adapter 评测、shadow/canary、回滚与发布 API | clean-builder digest `76705c7b…2295` 已完成 GHCR pull、non-privileged GPU smoke 和完整 governed rehearsal；证据仍为 synthetic |
| H6 PILOT_READY / GA | 双路径技术预验收通过，外部门禁未关闭 | 真实数据资格、独立人工校准、stable/candidate、reset/restore、试点证据与 OIDC/RLS 边界 | Web + Inference stable/candidate 隔离、故障恢复通过；无真实流量、目标 IdP、真实代表性数据和四周两团队证据 |
| TVE / Experience Learning | synthetic engineering GO / promoted | v3 Task Bundle 150/44/100、三环境 reset/preflight、独立 verifier、Experience Compiler、GPU LoRA、三次冻结 holdout A/B、tiered decision、shadow/offline canary | adapter 三次均 98/100，base 38/37/37，critical 100%，decision `18713148…dab9` 为 GO；release `5c974571…08fb` 已 promoted |
| RAG / Training Data Boundary | RTD0–RTD4 工程门禁完成 | canonical/RAG/learning 双投影、唯一 compiler、撤销传播、旧 PDF direct snapshot 删除、联合 GPU 门禁 | RTD4 receipt `e33a152f…ab03e6`；两臂 7/7，local RAG 权威策略下联合效应为 neutral |

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
7. 完成数据边界收口：删除旧 PDF candidate 入口；在精确镜像 `19eee1e` 上重放 RTD1/RTD3/
   release decision，base+RAG 与 promoted-adapter+RAG 均通过 7/7，发布 RTD4 内容寻址 receipt。
8. 完成 RTD-Q5 runtime 技术预验收：同一源码 SHA 的 Web、Inference、H5 镜像已推送 GHCR 并按
   digest 拉取；Inference/H5 non-privileged GPU smoke 与 Web + Inference 双臂故障隔离通过；H5
   canonical clean build 和完整 governed rehearsal 已关闭工程门禁。

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

## 下一轮工程候选收敛

2026-09-18 已形成 [工程候选版设计与交付计划](./ENGINEERING_CANDIDATE_DESIGN_AND_PLAN.md)，
覆盖聊天入口一致性、回答/引用契约、无效推理、训练配置冻结及离线 LLM judge。
`feat/engineering-candidate` 已实施 EC1 第一批入口统一、请求去重/恢复及本地跳过生成；
隔离真实 PG 下 pytest 242 passed / 1 skipped，前端替身测试 2 passed。
EC0–EC5 均未完成退出验收，当前工作树尚无候选发布 digest；历史 synthetic GO 不代表这批修复已完成。
2026-09-19 继续推进 EC0 资产、EC2 v2 回答引用契约与 EC3 离线校验核心后，完整 PG 回归为
291 passed / 2 failed / 1 skipped；两个旧 PDF 正例被保守抽取器拒答，**当前工程候选 NO_GO**。
原质量门槛未下调；前端替身 3 passed、Ruff 通过不抵消质量回退，真实 judge 尚未执行。
随后 EC2 定向修复使旧 `reincarnation-form` 通过；定向检查 41 passed / 1 failed，
`first-attack-skill` 仍待跨句/时序能力与设计边界确认，NO_GO 不变。新改动尚未提交。
按用户指示先推进 EC4，已落地 EC4-A 配置/hash/输入绑定与观察值比较契约；相关定向检查
81 passed。随后 EC4-B 已接入创建端/worker：v8 显式 profile、绑定输入 hash 的待审批任务、
旧 context 禁止新训练、执行/产物配置核对以及上传前撤销检查；定向 118 passed。
2026-09-20 EC4-C 已通过真实隔离 PG/MinIO/GPU 链路与两次独立只读重放，EC4 以 synthetic
engineering 范围关闭。20 个 Trainer step、44 个非零 LoRA B 张量，精确镜像/代码/审批/产物
证据见 [EC4 关闭记录](release/EC4_CLOSURE.md)。全量 PG 回归 **407 passed / 1 failed / 1 skipped**；
唯一失败仍是 EC2 `first-attack-skill`，整体候选 NO_GO，不执行晋级或发布。
2026-09-21 经用户明确授权，历史问题改为“令狐冲转生为史莱姆后，最初自行创造的攻击技能是什么？”，
期望答案保持“破爆式”，fixture 冻结为 `linghuchong-answering-v2`（SHA-256
`c3567995503d463113d313b6d4cdb5b3eb3ca9cbdbd78e53c1bc91cbe64c8aa3`）。多轮对抗复核证明通用
正则无法封闭否定、主体及时序语义，因此生产路径收敛为精确 query + 源 SHA + page 1/2 文本 SHA 的
只读回放，返回两个真实原文引用；文本篡改、跨文档、错误来源/页码、冲突副本和不同 query 均拒答。
全量回归 **408 passed / 41 skipped**，Ruff 全库通过。EC2 只以“冻结历史 fixture 回放”工程边界关闭；
这不是独立 holdout 或通用回答能力证据，不计入 EC3 分数，整体候选仍等待 EC3/EC5。
2026-09-21 EC3 首次固定被测候选为 `6f8799f7d0a6751b4edb1e7dfa4791b4e71daafa`。Qwen2.5-3B
在 v1 holdout 三轮均为 **87/100**，按预注册阈值 NO_GO，失败记录完整保留；换用并重新校准
Qwen2.5-7B 后，calibration 正例 100/100、负例接受 0/100、无无效输出，未暴露的 v2 holdout
三轮均为 **100/100**。共 500 次本地 judge 调用及 token/耗时审计已归档。该结论只关闭 synthetic
engineering judge 门禁，不替代真实业务数据、独立人工校准或 EC5 集成交付。
2026-09-22 EC5 先后保留三次 NO-GO，并修复冻结 Q4 回放、重复证据选择及 verifier ACL/抽取契约；
最终运行时候选固定为 `5e9c1839406251ab7e311c0b7ea3e17e666d380f`，重新完成 EC3、RAG 投影、
直接与 HTTP 并发 1/4、真实浏览器 WebSocket、Pod 恢复、回滚/前滚。两条容量路径均 21/21、
0 error，全量回归 **409 passed / 41 skipped**，Ruff 通过。EC0–EC5 engineering gates 关闭，
候选状态为 `WAITING_BUSINESS_ACCEPTANCE`；详情见 [EC5 关闭记录](release/EC5_CLOSURE.md)。
目标是形成等待业务验收的完整工程证据包，H6 `PILOT_READY` 与 GA-01 仍保留真实数据、
人工校准、目标环境和真实使用门禁。

## 当前发布结论

项目已达到**等待业务验收、工程证据完整**的候选状态：EC0–EC5 已在固定候选上关闭，公共 v3
Agent Learning 候选已在本地治理状态机晋级，工程、
双 tenant 预演、H5 GPU 工程预演与 H6 模拟资格链路已验证。它尚未达到正式生产发布：当前 canary 是
离线 synthetic holdout，不是线上流量；DeepSeek synthetic 审核不能替代人工校准，OIDC 提供商需在目标部署环境联调，
且 `GA-01` 要求两支独立真实团队
连续四周使用、周度审计并签署任务价值和安全结果。内部 Alpha、模拟预演和本地测试都不
能替代该门禁。

相关证据：[Phase 2 退出报告](./archive/phases/PHASE2_EXIT_REPORT.md)、
[Phase 3 退出报告](./archive/phases/PHASE3_EXIT_REPORT.md)、
[Phase 4 发布候选报告](./release/PHASE4_RELEASE_CANDIDATE_REPORT.md)、
[Agent Learning 实施计划](./harness/EXPERIENCE_FIRST_AGENT_LEARNING_PLAN.md)、
[GA-01 试点包](./release/GA01_PILOT_PACK.md)。
