# H6 `PILOT_READY` 模拟预演报告

> 本报告是 synthetic engineering rehearsal，不是正式试点验收，也不会关闭 `GA-01`。

- 工程预演结果：**PASSED**（7/7 checks）
- 模拟资格状态：`pilot_ready`
- 外部发布门禁：**BLOCKED**
- 模拟 tenant：`h6-synthetic-tenant`

## 预演覆盖

| 检查 | 结果 | 证据摘要 |
| --- | --- | --- |
| `synthetic_data_qualification` | PASSED | `{"acl_digest": "d2089a3c1fc20bf5a949bcccf367ef0789c69f7d76789217ca9df75e83a05eaa", "manifest_sha256": "a3ea82be8281506523045167e187b3bbeee9fe073abffa43ba255236dbfa1067", "permission_version": "h6-sim-permission-v1", "training_allowed": true}` |
| `independent_human_judge_calibration` | PASSED | `{"agreement": 1.0, "category_counts": {"acl": 1, "evidence": 1, "general": 1, "security": 1}, "false_accepts": 0, "false_rejects": 0, "hard_gate_categories": ["acl", "security"], "missing_hard_gate_categories": [], "passed": true, "policy_version": "h6-sim-policy-v1", "sample_count": 4}` |
| `stable_candidate_shadow_isolation` | PASSED | `{"authority": "stable", "candidate_observer": "candidate-sim-release"}` |
| `canary_failure_and_rollback` | PASSED | `{"canary_percent": 25, "error_rate": 1.0, "max_error_rate": 0.01, "result": "rolled_back", "window_complete": true}` |
| `shadow_side_effect_fault_injection` | PASSED | `{"error": "shadow_side_effects_forbidden", "rejected": true}` |
| `isolated_reset_restore_boundary` | PASSED | `{"actions": ["delete_kubernetes_jobs", "clear_postgres_test_schema", "clear_minio_prefix", "clear_redis_prefix"], "environment_id": "dataalchemy-gpu-test", "plan_sha256": "f7f6e17b9f7af43b44778ff0d83c95b5ed25f1f22330a454346a5bae91c8e45e", "reset_mode": "dry-run", "restore_destination": "dataalchemy_restore_test", "restore_source": "synthetic-backup", "source_database_untouched": true}` |
| `oidc_tenant_rls_boundary` | PASSED | `{"claims": {"role": "reviewer", "sub": "sim-user", "tenant_id": "h6-synthetic-tenant"}, "cross_tenant_read": "denied", "default_admin_login": "denied", "issuer": "https://synthetic-idp.invalid"}` |

## 不能由本预演关闭的门禁

- synthetic data is not real representative business data
- pre-labelled cases are not independent human review
- shadow/canary uses deterministic local routing, not production traffic
- reset/restore is dry-run only
- does not close H5 canonical image or GA-01
- two independent teams and four weeks of external evidence are missing

## 结论

H6 的资格状态机、校准 hard gate、stable/candidate 隔离、canary 故障回滚、reset/restore 边界和 OIDC/RLS 控制均通过模拟预演。该结果只能证明工程链路可执行；真实代表性数据、独立人工审核、H5 canonical 镜像和两支团队四周试点仍需外部完成。

复现命令：

```bash
.venv/bin/python scripts/run_h6_pilot_ready_rehearsal.py
```
