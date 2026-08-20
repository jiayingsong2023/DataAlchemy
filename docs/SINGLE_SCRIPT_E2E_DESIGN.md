# DataAlchemy 单入口、两阶段端到端串联设计

> 状态：代码路径与真实 k3d/ROCm GPU 工程预演已完成；registry-clean canonical
> 镜像、生产 canary、独立人工校准与外部验收尚未完成
>
> 目标：使用一个 CLI 入口完成两个可恢复阶段：先把 PDF 处理到 WebUI 可用，再在
> 用户提问、反馈审核后继续执行 H5 学习与发布。两阶段不是一次不可中断的长命令；
> 生产审批、真实 canary 和外部验收必须保留独立门禁。

## 1. 当前状态与边界

当前代码已具备以下工程能力：

- `scripts/run_pdf_full_cycle.py`：集群、PDF 试点任务、RAG、会话 Memory、反馈审核的
  固定入口编排；
- `scripts/run_h5_pdf_cycle.py`：读取 annotation、创建 snapshot、提交 LoRA/evaluation
  Job，并调用 release governance 的固定入口编排；
- `webui/app.py`：反馈审核后写入不可变 MinIO source 和 PostgreSQL
  `trajectory_annotations`；
- `/api/h5/adapters`、`/api/h5/releases`、`/api/models/status`、显式 release 的
  `/api/models/reload` 等 H5 API；H5 阶段会自动执行 reload 与 adapter-backed WebUI probe。

代码已经提供稳定的两阶段 CLI、durable H5 attempt/lease、审批暂停/恢复、固定幂等 Job、
动态 gate、active adapter/release 执行证据、adapter-backed WebUI 验证以及 secret-free
receipt。真实 k3d/ROCm GPU Job 已在本地 cache-backed 镜像上跑通，但不能替代
registry-clean canonical 镜像、生产 canary、人工校准和外部团队验收；这些门禁不能由
工程模式合成数据替代。

本设计不引入第二套调度器、通用插件框架或任意 shell 编排入口；复用现有
`agent_events`、`run_manifests`、H5 governance 和 Job service。

## 2. 用户体验目标

### 2.1 阶段一：到 WebUI 可用

```bash
.venv/bin/python scripts/run_pdf_full_cycle.py \
  --stage webui \
  --pdf data/input/pilot.pdf \
  --reset \
  --confirm-cluster-reset dataalchemy-gpu \
  --deploy \
  --environment engineering \
  --allow-auto-approve \
  --probe-question "请概括本文档的主要内容"
```

需要在阶段一部署前为需要 adapter 验证的租户显式设置
`H5_LORA_MODE=single_tenant_lora` 与 `MODEL_RELEASE_TENANT_ID=<tenant_id>`；未设置时系统
仍可完成 RAG，但会拒绝把发布后的 adapter 宣称为 WebUI 已生效。

`--probe-question` 可选；没有提供时使用稳定的内置 probe。阶段一的输入任务在
production 模式下也可能进入 `waiting_approval`；engineering 模式只有显式提供
`--allow-auto-approve` 才能自动推进。阶段一执行：

```text
reset cluster
  → deploy
  → migrate database
  → readiness checks
  → upload PDF
  → rough clean
  → fine clean
  → publish PostgreSQL
  → RAG probe
  → memory subsystem ready
  → WebUI ready
```

PDF 入库不会凭空生成长期 Memory。阶段一只验证 Memory 服务可用，并将
`memory_distillation` 标记为 `waiting_for_conversation`。用户随后在 WebUI 中提问、查看
引用、提交反馈；会话关闭或达到蒸馏条件后，才执行：

```text
conversation
  → distillation
  → memory candidate
  → policy / conflict check
  → persisted memory
```

脚本输出并持久化：

- 根 `run_id`；
- WebUI URL 和认证/租户上下文摘要；
- 每个 gate 的状态、时间、evidence、artifact key/hash；
- deployment receipt；
- 下一步 H5 所需的 run reference。

阶段一成功的最低条件是：输入、rough clean、fine clean、publish、RAG probe 和 WebUI
健康检查通过。Memory 尚未有会话内容不是失败。

当用户尚未提供足够反馈或尚未关闭会话时，脚本返回 `waiting_input`，而不是失败：

```json
{
  "state": "waiting_input",
  "run_id": "...",
  "reason": "conversation_or_feedback_required",
  "next_action": "continue in WebUI, then rerun with --resume"
}
```

### 2.2 阶段二：审核后触发 H5

生产模式先创建候选并在审批处暂停：

```bash
.venv/bin/python scripts/run_pdf_full_cycle.py \
  --stage h5 \
  --run-id <run_id> \
  --suite data/input/pdf-suite.json \
  --environment production
```

第一次执行会创建一个 H5 attempt，并冻结 annotation 集合、suite、policy、模型、镜像和
代码版本。后续 `--resume` 默认只读取该冻结配置；重新传入的配置必须与冻结 hash 完全
一致，否则拒绝恢复。一个根 run 可以有多个历史 attempt，但同一时刻只能有一个 active
attempt。数据库必须通过唯一约束或事务 advisory lock 保证该条件，不能只依赖 CLI
进程内检查。每个 attempt 都有稳定的 `h5_attempt_id` 和可过期 lease；两个进程同时恢复时，
只有一个可以获得 lease，另一个返回 `already_running`。

脚本按该 `run_id`、tenant、来源版本和 ACL 选择已审核且
`training_allowed=true` 的 annotation。production 模式应支持显式
`--annotation-id` 选择并在执行前输出选择清单；engineering 模式才允许选择该 run 下全部
合格 annotation。若合格样本不足，返回 `waiting_input`，不得把正常的数据不足报告为系统
失败。脚本执行到下一个审批门禁后返回机器可读状态：

```json
{
  "state": "waiting_approval",
  "approval_type": "snapshot",
  "run_id": "...",
  "h5_attempt_id": "...",
  "next_action": "approve in WebUI, then rerun with --resume"
}
```

审核完成后恢复：

```bash
.venv/bin/python scripts/run_pdf_full_cycle.py \
  --stage h5 --run-id <run_id> \
  --attempt-id <h5_attempt_id> \
  --environment production --resume
```

未提供 `--attempt-id` 时，只允许恢复该根 run 唯一的 active attempt；没有 active attempt
或发现多个 active attempt 都必须拒绝并报告状态异常。

流程为：

```text
approved feedback
  → training snapshot
  → independent snapshot approval
  → base evaluation
  → GPU LoRA
  → adapter safety scan
  → adapter evaluation
  → shadow
  → measured canary
  → release approval
  → promote 或 rollback
  → WebUI model reload
  → adapter-backed WebUI verification
```

`scripts/run_h5_rehearsal.py` 只能用于 synthetic 工程预演，不能被 H5 生产阶段调用。

## 3. 模式、身份与审批边界

### 3.1 Production（默认）

- `--auto-approve` 和 `--allow-auto-approve` 直接拒绝；
- snapshot 和 release 必须由不同的、经过认证的 reviewer/promoter 完成；
- 审批是 durable state，不要求审批者保持脚本进程运行；
- 没有审批时只能进入 `waiting_approval`，不能自动 promote；
- promote 后必须记录 release、adapter、模型 reload 和最终回答的 execution evidence；
- 生产 canary 必须来自真实请求观测窗口，不能使用脚本构造的 sample、延迟或错误率。

### 3.2 Engineering

通过显式参数启用：

```text
--environment engineering --allow-auto-approve
```

工程模式可以自动推进隔离测试租户中的 snapshot 和发布预演，但所有记录必须标记：

```json
{
  "environment": "engineering",
  "external_acceptance": false,
  "approval_class": "automated_rehearsal"
}
```

工程自动批准不等于真实人工 maker-checker，也不等于生产审批。它仍必须保留不同的
automation owner/reviewer/promoter 记录，并继续执行 tenant、ACL、hash、固定评测和
回滚约束。该模式不得被用于宣称 GA 或真实团队验收通过。

Production CLI 不得使用默认管理员密码。应使用 OIDC 或短期 service token；token、密码和
完整数据库连接串不得写入 run receipt、MinIO manifest 或日志。

## 4. 根 Run 与数据血缘

一个 PDF 输入只创建一个根 `run_id`。所有对象都必须能通过该 ID 追溯，且每个跨边界对象
同时验证 tenant 和 ACL：

| 阶段 | 关联对象 | 必须验证 |
| --- | --- | --- |
| 输入 | descriptor、raw object | SHA-256、tenant、ACL |
| rough clean | Job、manifest | accepted/rejected、输入 hash、规则版本 |
| fine clean | normalized corpus | source version、chunk lineage、transform hash |
| publish | documents/chunks | PostgreSQL、ACL、FTS/vector |
| RAG | task evidence、citations | chunk/page 引用、source run |
| Memory | session、checkpoint、memory candidate | source event、状态、ACL、冲突结果 |
| 反馈 | MinIO source、annotation | reviewer、training permission、source run |
| H5 | snapshot/evaluation/adapter/release | snapshot、evaluation、release 状态和 hash |
| WebUI | reload/model status、final chat | active adapter、release、execution evidence |

各处理阶段必须形成连续的 hash chain，而不是仅各自记录一个 hash：

```text
raw.sha256
  = rough_clean.input_sha256
rough_clean.output_sha256
  = fine_clean.input_sha256
fine_clean.output_sha256
  = publish.input_sha256
published source/chunk hashes
  = RAG/feedback/snapshot source hashes
```

每个 manifest 至少记录 `input_artifact_id/hash`、`output_artifact_id/hash`、规则或代码版本、
producer image digest。独立 verifier 必须重新读取 MinIO/PostgreSQL/运行时状态验证这些值，
不能只信任编排脚本提交的 result JSON。

H5 attempt receipt 还必须冻结并保存：

```text
annotation_ids + annotation content hashes
suite_sha256
policy_version
base_model_digest + tokenizer_digest
job_image_digest
code_version
environment
```

H5 选择 annotation 时必须逐条验证：

- `annotation.run_id == root_run_id`；
- `annotation.tenant_id == root tenant_id`；
- 来源文档版本、source ACL digest 和内容 hash 一致；
- `status=approved`、`training_allowed=true`；
- reviewer、training purpose、permission version 非空且可审计；
- train/validation 划分可复现，不能将同一来源偷偷跨 split 泄漏。

H5 的 evaluation trial 可以有自己的内部 task ID，但必须携带 `root_run_id`，不能形成
无法追溯的第二条业务链。

## 5. WebUI 验证契约

Run Detail 页面从 durable evidence 动态计算以下 gates，不得返回硬编码的未来状态：

```text
input
rough_clean
fine_clean
publish
rag
memory
feedback_review
training_snapshot
base_evaluation
lora
adapter_evaluation
release
model_reload
webui_verification
```

统一 gate 状态：

```text
pending | running | waiting_input | waiting_approval | passed | failed | rolled_back
```

`GET /api/models/status` 必须返回至少：

```json
{
  "tenant_id": "derived-from-authentication",
  "release_scope": "single_tenant_lora",
  "base_model_digest": "...",
  "adapter_id": "...",
  "adapter_artifact_sha256": "...",
  "release_id": "...",
  "load_generation": 3,
  "loaded_at": "..."
}
```

`POST /api/models/reload` 必须显式指定要激活的发布，不得扫描或加载全局“latest adapter”：

```json
{
  "release_id": "...",
  "expected_adapter_id": "...",
  "expected_artifact_sha256": "..."
}
```

tenant 必须从认证身份获得，不能由请求体自由指定。服务端验证 release、adapter、artifact
都属于该 tenant。当前运行时按 `single_tenant_lora` 工作时，只允许配置的 release tenant
激活 adapter；其他 tenant 使用 base model 或被明确拒绝，不得共享全局 adapter 状态。

每次 `/api/chat` 的响应还必须包含本次回答实际使用的：

```json
{
  "model_execution": {
    "base_model_digest": "...",
    "adapter_id": "...",
    "release_id": "..."
  }
}
```

reload API 必须区分 `succeeded`、`already_current` 和 `failed`，不能把后两者都返回为
`skipped`。

H5 完成的必要条件：

```text
adapter.state = verified
release.status = promoted
model_reload.status in [succeeded, already_current]
active_adapter.tenant_id = release.tenant_id
active_adapter.adapter_id = release.adapter_id
active_adapter.artifact_sha256 = adapter.artifact_sha256
final chat.model_execution.release_id = release.id
final chat citations != empty
```

三类能力必须分别提供证据：

| 能力 | 最低证据 |
| --- | --- |
| RAG | 回答包含 PDF document/chunk/page 引用 |
| Memory | context 或回答 metadata 包含本次会话蒸馏出的 memory ID、来源事件和命中记录 |
| LoRA | 在关闭 RAG 或使用相同受控上下文时，adapter 相对 base 通过固定评测阈值 |

最终 WebUI 可以同时启用 RAG 和 adapter，但仅凭非空 citations 不能证明 LoRA 或 Memory
参与了回答。

## 6. 环境初始化与恢复

`--stage webui --deploy` 负责：

1. 检查并导入 core、operator、PostgreSQL、MinIO、Redis 镜像；
2. 仅对登记的测试集群执行创建或重建；
3. Helm 部署并等待 Operator、WebUI、PostgreSQL、MinIO、Redis；
4. 执行 migration 和基础健康检查；
5. 在进入 H5 前检查 GPU CDI/设备资源、ROCm/PyTorch/PEFT、模型目录和 immutable image
   digest；
6. 检查 rough-clean Job image、Spark Operator（若启用）和 MinIO 路径；
7. 写入 deployment receipt 和本次 run 的环境摘要；
8. 失败时保留 evidence，不删除可恢复的输入对象。

优先使用 Kubernetes Job 或容器内 migration。若本地必须使用 PostgreSQL port-forward，脚本
应负责启动、健康检查和清理；durable receipt 只记录 endpoint、开始/结束时间、进程结果，
不记录不可恢复的 PID 作为状态依据。

重置只能针对登记的测试集群，生产模式拒绝未登记目标。集群重置发生在业务 run 创建前，
应记录在 deployment receipt；重置中断后重新执行环境阶段，不伪造业务 gate 已通过。

恢复能力分为三级：

| 故障范围 | 恢复承诺 |
| --- | --- |
| CLI/WebUI Pod 中断 | 从 PostgreSQL gate 和 manifest 继续 |
| Job/节点失败 | 根据 lease、Job 状态和 MinIO artifact reconcile |
| 整个集群删除 | 只有 PostgreSQL 备份与 MinIO artifact 已放在集群外或完成恢复后才能 resume |

`--reset` 默认开始新的 deployment/run。若数据库和对象存储随集群删除，旧 run 必须标记为
不可恢复，不能仅凭本地 receipt 报告 resume 成功。

## 7. 失败、幂等与恢复

- 每个阶段先写 `running`，结束时写 `passed`、`waiting_input`、`waiting_approval`、`failed`
  或 `rolled_back`；
- Job 使用确定性幂等键
  `sha256(tenant_id + root_run_id + h5_attempt_id + gate_name + input_sha256)`；数据库以
  tenant、tool/gate 和该键建立唯一约束；
- attempt/gate 使用可过期 lease；并发 resume 只能有一个 owner，超时后由 reconcile 接管；
- `--resume` 只能恢复冻结的 H5 attempt；suite、模型、镜像或 policy hash 改变时拒绝恢复；
- 进程中断后按根 `run_id` 恢复最近一个未完成 gate；
- 已通过的 gate 不重复提交，只验证其 artifact/hash 是否仍存在；
- LoRA 或 adapter evaluation 失败时不创建 promoted release；
- canary 失败时自动 rollback 到 base 或上一个 promoted release；
- reload 失败或最终 adapter-backed chat 验证失败时，最终 gate 为 `failed`，并回滚 active
  adapter/release；
- `reload=already_current` 只有在 active adapter、release 和 artifact hash 全部匹配时才算
  通过；
- 来源文档删除、ACL 收紧或 training permission 撤销后，阻止新发布并对已发布 adapter
  生成 revoke/rollback 事件；
- 所有恢复动作写入事件和操作者/自动化身份。

adapter 激活必须先下载到 staging 目录并校验 artifact hash，再原子切换 active pointer；
不得在下载过程中覆盖当前可用 adapter。切换或最终问答验证失败时恢复原 pointer，并记录
rollback release、adapter 和 hash。

实现上优先复用 `agent_events`、`run_manifests` 和现有 H5 表；只有现有表无法表达 gate
历史时，才增加一个最小的 `run_gate_events` 表，不建立新的通用工作流服务。

CLI 的机器契约固定为：stdout 最后一行输出一个 JSON receipt；`passed`、`waiting_input`、
`waiting_approval` 和 `already_running` 都是可解释状态，只有输入非法、权限失败、数据损坏或
执行失败才返回非零退出码。每个 gate 还必须有 deadline、lease expiry 和取消后的 reconcile
规则，避免遗留无法接管的孤儿 Job。

## 8. 评测与 canary 要求

H5 固定评测集至少包含：

- `case_id`；
- `query`；
- 根据规范化输入和 policy version 重新计算的 `input_sha256`；
- 非空 `required_substrings`；
- suite version 和 policy version；
- 明确的 train/validation split。

suite 必须在 training snapshot 创建前锁定并写入 `suite_sha256`；snapshot 审批后不能替换
评测集。重新选择 suite 必须创建新的 H5 attempt。

训练问题与评测问题必须按 query hash 去重；评测集不能复用直接进入 training split 的
相同问答。样本不足以形成 train/validation 或还没有新反馈时，应返回 `waiting_input`。

评测集不能由模型自己生成，也不能使用空断言获得通过。固定 substring 断言可作为工程
smoke gate，但生产质量仍需校准后的指标和指定评测负责人。

query hash 只能发现完全相同的样本，生产评测还必须由评测负责人检查同义改写或答案泄漏。
单个 PDF 和少量 feedback 只能证明 `engineering_pipeline_evidence`，不能用于宣称 LoRA
获得了稳定泛化能力或生产质量提升。

Release canary 必须记录真实观测：

```text
sample_count >= configured minimum
window_seconds >= configured minimum
error_rate <= policy threshold
p95_ms <= policy threshold
security_passed = true
window_complete = true
```

工程故障注入可以验证 rollback，但合成观测只能标记为
`promotion_rehearsal`，不能写成真实 canary 或 external acceptance。

发布还必须满足固定 policy：adapter 不得触发安全硬门禁，不能低于 base 的关键质量指标，
并达到 suite 中定义的最小通过率/非退化阈值。阈值应冻结在 `policy_version` 中，不能由
待发布模型或脚本运行时生成。

## 9. 分阶段实施顺序

1. **S0：CLI 与状态契约**：实现 `--stage`、`--resume`、probe question、root run receipt
   和机器可读 gate 状态。
2. **S1：审批与血缘**：实现生产暂停/恢复、annotation 的 run/tenant/ACL 校验和可复现
   train/validation 选择。
3. **S2：模型生效证据**：实现动态 Run Detail、`/api/models/status`、chat execution
   metadata，以及 reload 三态结果。
4. **S3：环境与恢复**：实现 readiness、migration、deployment receipt、幂等恢复和受控
   故障注入。
5. **S4：验收矩阵**：覆盖 happy path、审批暂停/恢复、进程中断恢复、并发双 resume、
   跨 tenant annotation/release、suite hash 改变、伪 canary 拒绝、adapter hash 不匹配、
   active pointer 切换失败、reload 失败回滚，以及集群重置后拒绝伪恢复。
6. **S5：工程预演与真实验收**：执行本地 k3d PDF smoke、真实 GPU H5 rehearsal；生产
   canary、人工校准和外部团队试点单独记录，不能由本地模拟替代。

## 10. 最终退出条件

工程模式下，允许通过一次 WebUI 阶段和一次或多次可恢复的 H5 阶段完成：

```text
PDF → rough clean → fine clean → PostgreSQL/RAG → WebUI
→ 用户提问/Memory distillation → 审核 feedback
→ snapshot → LoRA → evaluation → promotion rehearsal
→ model reload → WebUI 使用 adapter 回答
```

工程退出必须同时提供：

- 根 run manifest 和所有 gate evidence；
- snapshot、evaluation、adapter、release 的 hash 和状态；
- active adapter status 与最终 chat execution metadata；
- rough/fine/publish/RAG/snapshot 的连续 hash chain 与独立 verifier 结果；
- 至少一次 rollback/recovery 证据；
- `external_acceptance=false` 的分类标记。

`full_cycle_complete` 还必须同时满足 Memory distillation gate；只有 H5 release/reload
通过而 Memory 尚未产生会话记录时，只能标记为 `h5_stage_complete`，不能标记完整产品
闭环完成。

生产模式只允许自动执行非审批步骤。正式 GA 仍需要真实业务数据、人工校准、真实请求
canary 和外部团队验收。
