# Phase 3 退出报告（工程门禁）

日期：2026-07-30；分支：`feat/phase-3-tooling-pilot`。

## 已完成的工程证据

- GitHub 只读连接器读取 commit 中的文本文件正文；新版本先写入 pgvector 文档，随后
  删除同一路径的旧版本。删除文件会立即撤销已发布文档与 chunk。
- 文档 ACL 使用现有 `document_acl` 与 PostgreSQL RLS；`GIT_PILOT_READERS` 作为显式
  用户读取快照，未配置时仅同步管理员可读。
- 工具网关限制每 tenant/工具一分钟调用量、只允许幂等工具声明重试预算，并对事件中的
  指定敏感字段脱敏；审批、幂等和失败路径保持既有持久化语义。
- WebUI 已提供任务审批/暂停/恢复/重试、连接器运行列表和记忆查询面板。
- `scripts/verify_pilot_restore.sh` 对隔离预创建数据库执行 pg_dump/pg_restore，并检查
  pgvector 与连接器游标/运行表；`scripts/evaluate_phase3_pilot.py` 执行五项工程任务集。
- `docs/DEPENDENCY_LAYERS.md` 固化 WebUI、检索、连接器、训练与开发依赖的镜像边界。

## 本地验证

`TEST_DATABASE_URL=... .venv/bin/pytest -q tests/test_agent_runtime.py tests/test_git_connector.py tests/test_runtime_tools.py tests/test_run_assets.py`

结果：12 passed（含工具限流/重试/脱敏、文件版本替换、ACL 与删除）。

`DATABASE_URL=... .venv/bin/python scripts/evaluate_phase3_pilot.py` 应输出
`task_success_rate: 1.0`；恢复演练需由拥有隔离目标库权限的发布环境执行，不能使用源库。

## 尚未满足的最终发布门禁

两团队连续四周真实试点、每周 ACL/审批/来源/恢复审计、真实任务指标和零安全事件均为
外部验收，尚未发生，不能由本地测试替代。因此 Phase 3 工程门禁可进入发布候选，最终
试点退出门禁仍为 **未通过**。
