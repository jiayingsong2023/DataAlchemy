# Phase 3 退出报告（工程门禁）

日期：2026-07-30；分支：`feat/phase-3-tooling-pilot`。

## 已完成的工程证据

- GitHub 只读连接器读取 commit 中的文本文件正文；后续收敛将原始对象先落入受限 MinIO
  接入区，通过类型、大小、编码、密钥和路径门禁后，才写入 pgvector 文档。新版本发布后
  删除同一路径的旧版本；删除文件会立即撤销已发布文档与 chunk。
- 文档 ACL 使用现有 `document_acl` 与 PostgreSQL RLS；`GIT_PILOT_READERS` 作为显式
  用户读取快照，未配置时仅同步管理员可读。
- 工具网关限制每 tenant/工具一分钟调用量、只允许幂等工具声明重试预算，并对事件中的
  指定敏感字段脱敏；审批、幂等和失败路径保持既有持久化语义。
- WebUI 已提供任务审批/暂停/恢复/重试、连接器运行列表和记忆查询面板。
- `scripts/verify_pilot_restore.sh` 对隔离预创建数据库执行 pg_dump/pg_restore，并检查
  pgvector 与连接器游标/运行表；`scripts/evaluate_phase3_pilot.py` 执行五项工程任务集。
- `docs/reference/DEPENDENCY_LAYERS.md` 固化 WebUI、检索、连接器、训练与开发依赖的镜像边界。

## 本地验证

`TEST_DATABASE_URL=... .venv/bin/pytest -q tests/test_agent_runtime.py tests/test_git_connector.py tests/test_runtime_tools.py tests/test_run_assets.py`

结果：12 passed（含工具限流/重试/脱敏、文件版本替换、ACL 与删除）。

`DATABASE_URL=... .venv/bin/python scripts/evaluate_phase3_pilot.py` 应输出
`task_success_rate: 1.0`。Phase 4 已在隔离恢复库 `phase4_restore` 完成包含治理表的恢复
演练；源库未写入，详见 [Phase 4 发布候选报告](../../release/PHASE4_RELEASE_CANDIDATE_REPORT.md)。

## 发布候选门禁：模拟试点预演

`scripts/evaluate_phase3_pilot_rehearsal.py` 已在两个隔离 tenant 中压缩执行四个周次。
每个周次、每个团队运行 10 项只读任务、一次审批工具调用和一次故障恢复；同时检查跨
tenant 任务与源内容不可见，并保留审计事件。预演通过后，Phase 3 可标记为**发布候选**。

最近一次结果：80/80 任务完成，8/8 审批与恢复成功，8 个周度审计窗口完整，跨 tenant
任务与源内容可见性均为 0。

## 尚未满足的最终发布门禁

两团队连续四周真实试点、每周 ACL/审批/来源/恢复审计、真实任务指标和零安全事件均为
外部验收，尚未发生，不能由本地测试替代。因此预演通过后 Phase 3 可进入发布候选，最终
试点退出门禁仍为 **未通过**。
