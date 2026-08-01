# H2 退出报告：统一运行证据与恢复

> 状态：H2 最终退出门禁 **通过**。分支：`feat/harness-h2-evidence-recovery`。
> 验收使用真实 k3d Kubernetes/Spark Job；没有用 FakeBackend 或本地 Spark 模拟替代。

## 已实现并在隔离 PostgreSQL 验证

- `010_harness_evidence_recovery.sql`：`run_manifests`、`harness_outbox`、`agent_jobs`、tenant RLS、
  published manifest 不可变 trigger 与 outbox/job 外键。
- strict task 在 final verifier 后经历 `evidence_pending → succeeded`；manifest 经过 staging、
  读回 hash 校验与 content-addressed final key 后才发布。未知 output 字段会阻断发布；secret
  字段不会进入证据。
- run manifest 按 `run_id` 关联任务契约、计划、ToolResult digest、verifier、Job、时间线和
  fingerprint。开发环境明确标为 `development_evidence`；生产缺失 Git/image/dependency fingerprint
  会阻断发布。
- `spark_rough_clean` 是唯一 Kubernetes Job ToolSpec。它冻结 input key/hash，返回持久 Job handle；
  Job `Complete` 后仍必须读取并校验 run-scoped MinIO result manifest，才会物化 ToolResult 与
  checkpoint。
- 取消已提交 Job 会先写 `cancel_job` outbox 并进入 `cancelling`；只在 Kubernetes 确认取消后进入
  `cancelled`。已提交 Job 不允许伪暂停。
- API/WebUI 可显示 evidence state/digest，查看已发布 manifest，并由管理员对 waiting Job 或
  evidence pending run 触发受控 reconcile。旧 `/api/jobs/full-cycle` 继续返回明确拒绝。

## 已通过的可重复检查

在全新隔离数据库 `phaseh2_clean` 上应用 `001`--`010` 迁移后执行：

```text
24 passed: tests/test_evidence.py tests/test_jobs.py
           tests/test_agent_runtime.py tests/test_runtime_tools.py
```

覆盖成功发布、secret 脱敏、跨 tenant 拒绝、final object 篡改阻断及重试、tombstone 删除、Job
结果 manifest 缺失/匹配、失败不推进 checkpoint、取消确认与迁移/RLS 基线。

## H2-GATE-01：真实 Job 验收（通过）

在 k3d `dataalchemy` 集群中，以固定输入 scope
`s3a://data-alchemy/raw/h2` 和 SHA-256
`7cafb81c95acf5f8917f5499e8cedbb074e18a0679b8a5aa611cce5752ac0399`，通过真实
`AgentRuntime → KubernetesJobBackend → Spark` 路径执行 strict task。

验收结果：

- `data-alchemy-harness:latest` 镜像摘要：
  `sha256:432b2db8daf0f32e9de52550bfbd3e4b7b6fbc2e92f0ff079fde5fd6ea3f2903`。
- 镜像内预置并校验三个离线 JAR：`hadoop-aws-3.3.4.jar`、
  `aws-java-sdk-bundle-1.12.262.jar`、`wildfly-openssl-1.0.7.Final.jar`；构建阶段固定
  SHA-256，运行日志显示 `Found 3 local Spark jars` 和 `Maven packages will be skipped`。
- Job `da-ade6174d-dc141153-a1` 成功完成，写出 `cleaned_corpus.jsonl`、`rag_chunks.jsonl`
  和 `runs/ade6174d-3a5c-4fe4-8bb4-fbaede225b30/jobs/59f7eb4b-6967-46dc-92c9-402ed14b208f/result.json`。
- result manifest 的 `tool_result` 通过 `verify_rough_clean@1`；Job result SHA-256 为
  `0c8aade7cab1a1fae8cba5aae6c3250fe612ef1bc6057596865f30dd55ec8432`，验证结论为
  `passed`。
- strict task `8e149139-7e7a-47ab-aeba-bfcfb4b86098` / run
  `ade6174d-3a5c-4fe4-8bb4-fbaede225b30` 完成 `waiting_job → evidence_pending → succeeded`。
- 最终 evidence manifest 已发布到 MinIO，并以读回 hash 校验：
  `evidence/h2/ade6174d-3a5c-4fe4-8bb4-fbaede225b30/manifests/sha256/41aec93c6df5025205ab64031930b207fcb36da7e043647510edd2aa8794efc1.json`
  （8988 bytes，SHA-256 `41aec93c6df5025205ab64031930b207fcb36da7e043647510edd2aa8794efc1`）。

本次收尾同时修正了两个真实路径问题：Kubernetes Job 显式继承受控对象存储配置并默认使用
预置镜像 `imagePullPolicy=Never`；只读 verifier 获得 `agent_tasks/agent_jobs` 最小查询权限；
`spark_rough_clean` 的 `output` 字段纳入 evidence 脱敏策略。

因此 H2-GATE-01 已关闭，H2 工程退出门禁全部通过。训练、评测、LoRA 和真实外部团队验收仍按
后续 H5/H6 门禁执行，不能由本次 k3d 验收替代。
