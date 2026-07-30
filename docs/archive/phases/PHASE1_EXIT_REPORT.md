# Phase 1 退出报告：单智能体运行时

> 验收日期：2026-07-30
> 分支：`feat/phase-1-agent-runtime`
> 任务基准：[任务定义](../eval/phase1_real_tasks.yaml)；[结果](../eval/phase1_real_task_results.json)

## 结论

Phase 1 退出门禁通过。系统采用单一 SQLite Agent Runtime；不采用 LangGraph，也不引入第二套运行时。

> 当前状态更新（2026-07-30）：本结论记录 Phase 1 验收时的实现。Phase 2 已将该运行时
> 的任务、事件、审批和工具幂等权威存储迁移到 PostgreSQL；项目仍保持单一自研运行时，
> 未引入 LangGraph。详见 [发布状态](../../RELEASE_STATUS.md)。

## 退出证据

| 门禁 | 证据 | 结果 |
| --- | --- | --- |
| 真实任务完成与事件 | 5 个真实只读 RAG 任务，均产生 `planned → started → observed → replanned → completed` | 5/5 成功，完成率 100% |
| 固定流水线对比 | 每个任务先调用固定 `Coordinator`，再调用 `rag_chat` 工具路径 | 已记录两条路径延迟与回答哈希；首项固定路径冷启动 288,957 ms，其余固定路径 7,465～9,441 ms，运行时路径 7,338～8,664 ms |
| 检查点与恢复 | 自动化测试覆盖持久化重启、暂停恢复和暂时故障重试 | 通过 |
| 审批与审计 | 实际 `evaluate` 工具任务在批准前停在 `waiting_approval`；批准后成功 | 事件包含 `approval_requested`、`approval_granted` 与 `completed` |
| 越权与幂等 | 自动化测试覆盖跨 tenant 拒绝、当前角色授权及幂等写工具 | 通过 |
| 单智能体基线 | 5 个真实任务、5 个运行时控制面场景 | 通过；未引入多智能体 |

## LangGraph 决策

使用 LangGraph 0.6.11 的隔离探针验证了 Human-in-the-loop 中断/恢复与异步调用。默认 `MemorySaver` 仅在进程内保存状态；跨进程恢复需要增加 durable checkpointer 与额外依赖。项目现有 SQLite Runtime 已提供持久化、恢复、HITL 和 FastAPI 接入，故拒绝引入 LangGraph，避免两套运行时和状态源。

## 验证命令

```bash
.venv/bin/pytest -q
.venv/bin/python scripts/evaluate_phase1_baseline.py
.venv/bin/python scripts/evaluate_phase1_real_tasks.py
helm lint deploy/charts/data-alchemy
helm template data-alchemy deploy/charts/data-alchemy >/dev/null
```
