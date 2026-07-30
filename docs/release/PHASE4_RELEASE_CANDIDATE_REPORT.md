# Phase 4 发布候选报告

分支：`feat/phase-4-governance-learning`。

## 工程证据

- OIDC 授权码 + PKCE、签名 state、服务器端 tenant/group→role 映射；生产环境要求
  `AUTH_MODE=oidc`，本地密码登录被关闭。
- PostgreSQL RLS 审计事件覆盖任务、工具、连接器、记忆策略与发布状态，记录会脱敏。
- 记忆到期策略产生可回放事件并可恢复为已批准状态；显式删除仍沿用既有不可逆删除路径。
- 发布状态机要求通过评测与回滚目标，支持候选、影子、灰度、晋级和回滚；已完成一条
  正常晋级及一条由错误率门槛触发的自动回滚工程周期。
- 隔离 PostgreSQL 恢复演练已恢复 `audit_events`、`memory_policy_events`、`release_records`
  和 pgvector 扩展；恢复库为 `phase4_restore`，源库 `phase2_clean` 未写入。

## 发布候选门禁

内部 Alpha 与 Phase 3 双 tenant 四周预演、Phase 4 两个发布周期、RLS/pgvector 回归和
Helm 渲染必须全绿。运行：

```bash
DATABASE_URL=... uv run python scripts/evaluate_phase4_internal_alpha.py
```

## 未替代的正式发布门禁

`GA-01`：两支真实团队连续四周试点、周度审计和双方签署。详见
[GA01_PILOT_PACK.md](./GA01_PILOT_PACK.md)。在此之前本项目只能称为发布候选，不得称为
正式生产验收通过。
