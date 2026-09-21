# EC4 配置一致性关闭记录

2026-09-20：EC4-A/B/C **关闭（synthetic engineering）**。不等于整体候选 GO、业务验收、人工审核或 GA。
实现位于 `feat/engineering-candidate` 未提交工作树；源代码内容指纹固定，不以旧 HEAD 冒充当前构建。

## 证据

- [机器可读记录](EC4_CLOSURE_EVIDENCE.json)：input/config/model/code/image hashes、审批事件、Job、Pod、产物、成本、两次重放。
- [真实 GPU 日志](EC4_GPU_SMOKE.txt)：保留初始 FP16 溢出及 AMD SDPA experimental 运行时警告，没有隐藏警告。
- [冻结 smoke profile](EC4_SYNTHETIC_TRAINING_PROFILE.json)：TinyLlama FP16 LoRA，44 个精确 q/v 模块，r16/alpha32、20 步、256 长度、seed42。

| 项目 | 结果 |
|---|---|
| 执行镜像 / Pod imageID | `sha256:4df3819b46352981898954f51ac004f8748fe2c794a850ff2449c9974ec70cee` |
| `src/**/*.py` 内容指纹 | `6790622af7735fa66202d7e076da4b27ab1a3fe13cec7a03fd495392e1835104` |
| task / run | `fc1a427c-ee4f-4ec1-97f5-a6d0080373e6` / `d644fcbb-2d99-4096-97d3-27f00d49fce1` |
| adapter | `784fcdaa-3f87-4a9d-9582-56d595e07588` |
| GPU / step / token | AMD Radeon 8060S / 20 Trainer steps / 5120 tokens |
| 显存峰值 / worker wall time | 3,172,560,896 bytes / 15.420535 s |
| 权重 | 88 个有限张量；44 个 B 张量全部非零 |
| 独立 verifier | 运行时通过；SELECT-only 非超级用户角色两次重放通过，summary 完全相同 |
| MinIO 条件写 | `IfNoneMatch="*"` 重写返回 HTTP 412，原对象 `first` 不变 |
| 回归 | 定向 140 passed；全量真实 PG 407 passed / 1 failed / 1 skipped |

20 个 global step **不是** 20 次优化器更新：初始 FP16 overflow 后恢复正常梯度，最终 B 权重已偏离标准零初始化。
这是执行和配置一致性证明，不是业务训练有效性或泛化质量证明。GPU 硬件/安全上下文证据来自实际 Pod，
配置 verifier 本身保持 `gpu_execution_verified=false`、`independent_execution_verified=false`，不伪造远程硬件证明。

## 重放

配置 `VERIFIER_DATABASE_URL` 为证据数据库的 SELECT-only 角色，配置 `S3_ENDPOINT`、
`S3_BUCKET=ec4-evidence`、`AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`；凭据不写入仓库。

```bash
UV_NO_SYNC=1 uv run python scripts/verify_training_configuration.py \
  --tenant-id h5-simulation-39f39288-0e0d-47fb-a6ef-db2159546216 \
  --adapter-id 784fcdaa-3f87-4a9d-9582-56d595e07588
```

成功退出 0；失败退出 1。此命令重读原 PG/S3 证据，不仅验证仓库 JSON 自洽。
完整原输入包含运行用数据库 URL，因此公开 JSON 仅导出脱敏 projection；记录中的 input hash 指向原对象。
原证据保留在本任务隔离的 `dataalchemy-ec4-postgres` / `dataalchemy-ec4-minio`，未删除。
Kubernetes namespace `dataalchemy-ec4` 中 Job 有 TTL；Pod 摘要和完整日志已在自动清理前归档。
仓库记录不含模型权重或数据库备份；若迁移/删除上述服务，须先导出对应 PG/S3，否则不能完整重放。

新环境重跑使用 `scripts/run_h5_rehearsal.py --model-dir data/models/TinyLlama
--training-profile docs/release/EC4_SYNTHETIC_TRAINING_PROFILE.json --training-only`，需显式设置
固定 digest 的 `HARNESS_JOB_IMAGE`、隔离 DB/S3、GPU namespace 和本地模型挂载。
此命令仅对新建合成租户自动审批，不适用于真实业务数据；真实创建端仍停在 `waiting_approval`。

```bash
UV_NO_SYNC=1 uv run pytest -q tests/test_training_config.py tests/test_training_worker.py \
  tests/test_training_verifier.py tests/test_h5_evaluation.py tests/test_jobs_backend.py \
  tests/test_h5_pdf_cycle.py tests/test_runtime_tools.py
# 全量：TEST_DATABASE_URL 指向完成 migrations 的隔离 PG，使用非超级用户应用角色。
UV_NO_SYNC=1 uv run pytest -q
```

## 失败尝试与边界

1. 首次真实训练在 PEFT 自动缩写 target_modules 时被配置检查拦截；改为核验实际注入模块后记录完整名称。
2. 第二次两步训练运行结束，但 runtime 的双层 output 信封导致 verifier 拒绝；日志还显示梯度 NaN。
   未将该次结果当成功证据，补充全零 B 拒绝，并为新尝试显式审批 20 步 profile。
3. 最终运行完成，审批先于 Job 请求，执行期无取消；原始证据不被后续修复覆盖。

唯一全量失败仍是 EC2 的 `first-attack-skill`；skip 是 opt-in 的破坏性 TVE 测试。
既有 3 项依赖/日期 API 弃用警告，以及 AMD SDPA runtime warning，不构成生产 kernel 资格证明。
当前无新 kernel 开发、无新依赖；采用既有 Trainer/PEFT/治理链，不增加另一套训练框架。
EC2、EC3、EC5 和业务/人工门禁不因本记录关闭。未发布、未晋级、未提交或推送。

## EC2 集成树重验证（2026-09-21）

EC2 最终源码改变了镜像内容，因此旧 `4df3819b…70cee` 仅保留为历史关闭证据，不能代表统一候选。
当前树已重建并导入不可变 OCI digest `sha256:2da48493…b37848`，`src/**/*.py` 指纹为
`1978d88d…aafbc`。同一冻结 profile 在真实 PG、MinIO 和 AMD Radeon 8060S 上再次完成 20 step、
5120 token 训练；Pod imageID 与 digest 一致，adapter 权重 hash 与历史成功运行一致。

运行时配置 verifier 通过，随后 SELECT-only verifier 两次重放 summary 完全相同；EC4 定向
**140 passed**，当前全量 **408 passed / 41 skipped / 0 failed**，Ruff 与 diff check 通过。
首次 Pod 因本地 containerd 尚无 digest alias 暂停在 `ErrImageNeverPull`；补充同一 OCI manifest 的
不可变 alias 后原 Pod 启动并成功，没有重建或换用可变 tag。完整当前记录见
[候选重验证 JSON](EC4_CANDIDATE_REVALIDATION.json) 与
[候选 GPU 摘要](EC4_CANDIDATE_GPU_SMOKE.txt)。该证据继续只关闭 synthetic engineering EC4。
