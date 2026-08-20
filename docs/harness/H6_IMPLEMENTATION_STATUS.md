# H6 工程状态报告

集成状态：原实施分支 `feat/harness-h6-pilot-ga` 已经 `feat/harness` 合并到 `main`；
以下分支名仅表示历史验收上下文。

最新 synthetic `PILOT_READY` 预演：**7/7 checks passed**。可用以下命令重复：

```bash
.venv/bin/python scripts/run_h6_pilot_ready_rehearsal.py
```

完整证据见 [H6 PILOT_READY 模拟预演报告](./H6_PILOT_READY_REHEARSAL_REPORT.md)。该报告只关闭工程模拟门禁，不改变真实外部发布状态。

## 已实现

- `qualification_records`：数据授权、ACL/许可/suite hash、人工 reviewer、校准与撤销状态机；撤销会传播到 snapshot、adapter 和 active release。
- `CalibrationPolicy`：人工标签与 judge 标签的最小样本、一致率、误接受和 security/ACL hard gate 聚合，缺字段 fail closed。
- `DeploymentBinding`：stable/candidate 不可变 digest、shadow 只读、确定性 canary 分流和 side-effect 禁止；新增 `verify_deployment_binding@1`、`verify_shadow@1`。
- `pilot_programs` / `pilot_evidence_records`：团队、周度审计、事件和签署证据的 tenant-RLS 数据模型与 API。
- `reset_pilot_environment.py`：只接受预注册 test 环境；默认 dry-run，执行需计划 hash 确认，并清理精确的 k3d Job、PostgreSQL 表、MinIO 前缀和 Redis 前缀。

## 当前验证

`.venv/bin/pytest -q tests/test_h6_qualification.py tests/test_h6_calibration.py tests/test_h6_deployment.py tests/test_h6_environment_reset.py tests/test_verifiers.py tests/test_h5_evaluation.py`：15 passed, 1 skipped（无本地数据库时跳过集成项）。

## 未关闭门禁

- `PILOT_READY`：必须用真实代表性数据、独立人工校准、可重建 H5 canonical 镜像、目标 IdP 和真实 shadow/canary 证据执行；当前 synthetic/本地 cache 镜像只能证明工程代码，不得宣称通过。
- `GA-01`：两支独立真实团队连续四周、每周审计、价值/安全签署。当前没有真实团队，因此明确为 `blocked`，不能用模拟数据或 LLM 自评替代。
