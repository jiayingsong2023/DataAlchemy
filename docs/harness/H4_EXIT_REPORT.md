# H4 退出报告：Context、记忆与冲突治理

> 工作分支：`feat/harness-h4-context-memory`。基线：`feat/harness` 的 `3971342`。
> H4 已通过工程退出门禁；H5 的轨迹规模化、LoRA 和发布治理仍未开始，H6 的真实外部试点也不在本报告范围内。

## 1. 交付结果

| 目标 | 实现位置 | 结果 |
| --- | --- | --- |
| PostgreSQL append-only transcript | `011_harness_context_memory.sql`、`src/memory/context.py` | 会话、事件、generation、CAS version、RLS 已实现；Redis 仅作为兼容投影。 |
| Context/Skills 包 | `src/harness/context_packs/*.json` | `chat_rag@1`、`document_product_loop@1`、`memory_distillation@1` 按任务选择并记录 hash。 |
| ContextEnvelope 与预算 | `ContextService.build_context` | 记录 pack、事件、chunk、memory 引用、token 预算和 envelope hash，不保存隐藏推理。 |
| Compaction / reset / handoff | `ContextService.compact/reset/resume` | 原始事件不删除；checkpoint 有来源 digest；恢复重新校验 tenant、身份和 task/plan 参数。 |
| Memory distillation | `extract_candidates`、`distill_memory_candidates` | 会话关闭/显式 distill 触发；候选必须引用原始 conversation event。 |
| 分级写入 | `MemoryOrchestrator.create_governed_candidate/approve/reject` | 个人低风险可自动批准；shared 需管理员且不能 owner 自批；敏感和不可信来源拒绝。 |
| 去重与冲突 | `MemoryOrchestrator`、`MemoryGovernance.resolve_conflict` | exact dedupe、claim conflict、管理员裁决、supersede 和 policy event 已实现。 |
| 过期、撤权、删除 | `MemoryGovernance`、现有 deletion path | retrieval 只返回 approved 且未过期记录；来源撤销可 supersede 仅依赖该来源的记忆。 |
| 独立 verifier | `src/core/verifiers.py` | 新增 snapshot、checkpoint、distillation、policy verifier，并使用 H1 只读服务。 |
| AgentRuntime 工具 | `src/core/runtime_tools.py` | `compact_context`、`distill_memory_candidates`、`apply_memory_policy` 复用唯一运行时。 |
| WebUI/API | `webui/app.py` | session、context、close/reset/resume、distill、memory preview/decision/conflict API 已接通。 |

## 2. 关键安全结论

- 聊天内容首先落 PostgreSQL；Redis 丢失不会丢失会话事实。
- compaction 只产生摘要和 handoff，不能替换 transcript，也不能单独成为记忆来源。
- 外部文本、模型输出和 legacy 记录不能自动产生可信长期记忆。
- `/api/memories/{id}/approval` 已移除普通用户审批 shared memory 的旁路；拒绝是状态决策，不再伪装成删除。
- sensitive、credential、auth、access-control、health、financial、legal 等类别固定拒绝。
- 无权威规则的同 `claim_key` 不同值保持 `conflicted`，默认检索不返回，必须管理员裁决。

## 3. 验证证据

本地隔离 PostgreSQL `dataalchemy-phase2-pg` 已应用 `011_harness_context_memory.sql` 和
`012_harness_context_acl.sql`，使用独立
`dataalchemy_app` 与 `dataalchemy_verifier` 连接执行：

```bash
export TEST_DATABASE_URL='postgresql://dataalchemy_app:***@127.0.0.1:55432/dataalchemy'
export DATABASE_URL="$TEST_DATABASE_URL"
export VERIFIER_DATABASE_URL='postgresql://dataalchemy_verifier:***@127.0.0.1:55432/dataalchemy'
.venv/bin/pytest -q
```

覆盖结果：

- H4 unit/integration：Context pack、候选提炼、tenant RLS、Redis-independent transcript、
  compaction/reset、自动个人记忆、敏感拒绝、shared 审批和冲突裁决全部通过。
- H0--H3 既有测试与 H4 组合测试在配置完整的隔离库中通过。
- `python3 -m py_compile` 覆盖新增 Python 模块和 WebUI；`git diff --check` 通过。
- 生产配置仍要求独立 `VERIFIER_DATABASE_URL`；未配置 verifier 的生产校验失败是预期安全行为。

## 4. H4 退出门禁

- [x] PostgreSQL transcript 是会话权威，Redis 仅 TTL 投影。
- [x] ContextEnvelope、pack version、预算、checkpoint、handoff 可查询并有 hash。
- [x] reset/resume 对 tenant、identity、来源、TaskSpec/plan 参数执行 fail-closed 校验。
- [x] 个人低风险自动记忆、shared 管理员审批、敏感拒绝和自动记忆开关通过。
- [x] 去重、冲突、supersede、过期、来源撤销、修订和删除路径通过。
- [x] snapshot/checkpoint/distillation/policy verifier 已注册并使用独立只读服务。
- [x] prompt injection、跨 tenant、普通用户越权审批、Redis 故障和中断恢复轨迹已覆盖。
- [x] H3 WebUI memory gate 已从固定 `blocked_by_phase` 改为 H4 运行后可查询状态；H5 training/release 仍保持阻塞。
- [x] 没有用外部团队试点或模拟客户结果替代 H4 工程门禁；真实团队验收仍属于 H6/GA-01。

## 5. 后续工作

H4 完成后应将 `feat/harness-h4-context-memory` 的改动提交并合并回 `feat/harness`，再从更新后的
集成分支创建 H5。H5 负责轨迹规模化、训练快照、LoRA 固定评测和受控发布，不能由本报告预先宣称完成。
