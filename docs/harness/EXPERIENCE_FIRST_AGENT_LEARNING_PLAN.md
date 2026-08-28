# Task-Environment-Verifier-first Agent Learning 实施计划

> 状态（2026-08-28）：公共 v3 synthetic 全链已执行至 engineering GO；EL-3 三次冻结 holdout
> 均为 98/100，adapter 已 verified，release 已 promoted。EL-4/EL-5 为 `not-enabled`，Agent
> Lightning 为 `not-selected`。代码基线：`feat/harness-tve`。
> DeepSeek V4 已替代本轮人工初审，但 `human_reviewed=false`；本结果不能关闭生产人工校准与发布门禁。
> 设计依据见
> [Task-Environment-Verifier-first Agent Learning 设计](./EXPERIENCE_FIRST_AGENT_LEARNING_DESIGN.md)。
> 本计划不改变 [当前发布状态](../RELEASE_STATUS.md)；每个工作包只有通过自己的真实退出门禁后
> 才能标记完成。

## 1. 实施原则

- 先建立可重复执行、可独立判卷的 Task → Environment → Verifier（TEV），再把 rollout 发布为
  Experience；
- 复用 `AgentRuntime`、`VerifierSpec`、H2 evidence、H5 evaluation、H6 环境注册/reset 和
  PostgreSQL + MinIO，不建设第二个 runtime、Environment 平台或 Experience Store 服务；
- 第一条纵切只做现有 PDF/RAG：有证据回答，无证据 abstain；
- 环境或 verifier 故障记为 `invalidated`，模型在有效环境中的行为错误记为 `failed`；
- 第一版 compiler 只实现 SFT；DPO、RL 与 Agent Lightning 默认为 `not-enabled`；
- 数据库/MinIO、真实 reset 或真实模型集成测试被跳过时，相关工作包不能关闭；
- 每个工作包保持最小提交范围，不用 mock、synthetic 或弱化 verifier 替代真实退出门禁。

## 2. 总体顺序

```text
TVE-0 契约冻结
  -> TVE-1 Task Bundle
  -> TVE-2 Environment reset/preflight
  -> TVE-3 独立 Verifier
  -> TVE-4 双模型 re-rollout
  -> EL-1 Experience 捕获
  -> EL-2 Gap analysis + Experience Compiler + SFT
  -> EL-3 跨模型受控 A/B
  -> EL-3R 发布证据修复 + model-specific SFT 重训
  -> EL-4 DPO 条件决策
  -> EL-5 RL / Agent Lightning 条件决策
```

TVE-0--TVE-4 是模型无关的最小可执行资产闭环；没有通过 TVE-4，不开始 Experience Compiler 或训练。

## 3. TVE-0：冻结契约与失败分类

**状态：** `validated`（2026-08-23）。已实现 canonical validator、脱敏 fixture 和现有 verifier
类型投影；定向 Ruff、11 项契约/H5 状态回归以及全仓 81 项测试通过。全仓另有 37 项既有外部
集成测试因环境缺失跳过，不用于证明 PostgreSQL/MinIO/reset 能力；TVE-1 已实现但真实集成门禁未关闭。

**目标：** 使用现有数据结构固定 Task、Environment 和 Verifier 的最小契约，不修改模型行为。

### 3.1 工作项

1. 在 `src/harness/experience.py` 复用 `core.evidence.canonical_bytes/sha256`，实现内容寻址
   `task_bundle.v1` validator；不引入 Pydantic、schema registry 或新服务。
2. 固定 Environment receipt 投影：environment ID、registry/reset plan/fixture/image/tool hash、reset
   receipt、preflight、initial state、final delta 和 cleanup 状态。
3. 复用 `VerifierSpec`/`VerificationResult`，固定 status、hard gates、scores、failure code、evidence refs、
   verifier version 和 contract digest。
4. 固定状态分类：`succeeded`、`failed`、`invalidated`、`aborted`；列出稳定 reason code。
5. 固定数据边界：模型可见输入不得包含 expected answer、隐藏断言、verifier credential 或跨 tenant ref。
6. 增加脱敏 fixture，覆盖成功、模型失败、环境 invalid、verifier invalid 和越权拒绝。

### 3.2 预计文件

- `src/harness/experience.py`
- `src/core/verifiers.py`
- `tests/test_experience.py`
- `tests/fixtures/experience/*.json`

### 3.3 退出门禁

- canonical JSON 重复序列化得到相同 digest；
- 缺失 hash、未知 secret、跨 tenant ref 和隐藏答案泄漏均 fail closed；
- 环境/verifier 故障不能被分类为模型失败；
- fixture 不包含真实凭据、prompt 或业务正文。

### 3.4 提交范围

`test/harness: freeze task environment verifier contracts`

只包含契约、validator 和测试，不改 WebUI、模型、reset 执行或数据库 schema。

## 4. TVE-1：建立不可变 Task Bundle

**状态：** `implemented`（2026-08-23）。已实现 PDF/RAG case 的四对象内容寻址发布、隐藏 verifier
criteria 隔离、trial fingerprint 强制绑定和 `verify_task_bundle@1`；定向 23 项及全仓 86 项测试通过。
真实 PostgreSQL + MinIO 上的多 run/trial 门禁尚未执行，故未标记 `validated`。

**目标：** 将可重复使用的任务与某次 run、旧模型回答和训练数据解耦。

### 4.1 工作项

1. 从现有 H5 evaluation case 生成 `task_bundle.v1`，冻结 case input、source/fixture ref/hash、
   initial-state ref、tool contract、limits、verifier contract、tenant/ACL、许可和 split。
2. Task Bundle 使用 canonical content hash 作为 `task_bundle_id`；每次实例化创建新的 `run_id/trial_id`。
3. 明确 train/validation/evaluation holdout；隐藏断言只对 verifier 可见。
4. 第一批 bundle 只覆盖 PDF/RAG：固定 PDF hash、问题、tenant/ACL，以及 answer-with-citation 或
   abstain 的成功标准。
5. 复用 `trajectory_trials.fingerprint_json` 和 MinIO 保存 bundle ref/hash；JSONB 够用时不新增表。
6. 增加 `verify_task_bundle@1`。
7. H5 worker context 将 model-visible `cases` 与 verifier-only `verifier_cases` 分离；新 PDF cycle
   必须显式提供 `--task-retention-until`。

### 4.2 预计文件

- `src/harness/experience.py`
- `src/harness/evaluation.py`
- `src/harness/evaluation_runner.py`
- `src/harness/jobs.py`
- `src/core/verifiers.py`
- `scripts/run_h5_pdf_cycle.py`
- `scripts/run_h5_rehearsal.py`
- `tests/test_experience.py`
- `tests/test_h5_evaluation.py`

### 4.3 退出门禁

- 同一 case 重复生成同一 bundle digest；修改 input、fixture、tool 或 verifier 任一项都会改变 digest；
- bundle 不保存旧模型 answer、trajectory 或目标 tokenizer；
- holdout、ACL、许可和 retention 缺失时 verifier 拒绝；
- 一个 bundle 可创建多个相互独立的 run/trial。

### 4.4 提交范围

`feat(harness): publish immutable task bundles`

不新增 task registry 表；只有真实查询和 FK 需求证明 JSONB/ref 不足时再迁移。

## 5. TVE-2：Environment reset、preflight 与隔离

**状态：** `validated`（2026-08-23）。预注册 `dataalchemy-gpu-test` 已真实执行三次 reset，三次
`initial_state_sha256` 一致；PostgreSQL RLS 目标 tenant 可读且非目标 tenant 为 0 行，MinIO PDF 与
Redis fixture 哈希一致，Kubernetes workload 全部 ready，source permission 撤销会拒绝 preflight，
final delta/cleanup receipt 已发布。cleanup 后专用 schema、MinIO fixture prefix 和 Redis prefix 均为空，
tenant evidence prefix 保留不可变 receipt/preflight 对象。显式集成门禁 `1 passed`；默认全仓
`88 passed, 38 skipped`，其中 destructive 集成测试默认 skip，必须携带精确确认值才执行。

**目标：** 同一 Task Bundle 每次从可验证的等价初始状态开始。

### 5.1 工作项

1. 扩展现有 `deploy/pilot-environments.example.yaml` 和 `scripts/reset_pilot_environment.py` 的证据输出，
   保留预注册测试目标、默认 dry-run、精确确认和 production/shared 拒绝规则。
2. reset 后恢复 PDF/RAG fixture，并记录 PostgreSQL、MinIO、Redis、namespace、image、source object 和
   tool/dependency 的 ref/hash。
3. 增加最小 preflight：服务健康、fixture 存在、tenant/ACL 可读、非目标 tenant 不可读、reset target
   与 Task Bundle 一致。
4. 计算 `initial_state_sha256`；保存 reset/preflight receipt。运行后记录必要 final delta 和 cleanup 结果。
5. 每个 tenant/run 使用现有 namespace/database/prefix 隔离；secret 运行时注入，不写入 bundle/receipt。
6. reset、fixture、preflight、cleanup 或基础设施故障统一产生 `invalidated` 证据，不惩罚模型。

### 5.2 预计文件

- `deploy/pilot-environments.example.yaml`
- `scripts/reset_pilot_environment.py`
- `src/harness/experience.py`
- `tests/test_h6_environment_reset.py`
- `tests/test_experience.py`
- `tests/test_tve2_environment_integration.py`

### 5.3 退出门禁

- 同一 bundle 连续 reset 三次得到相同 `initial_state_sha256`；
- 任意 production、shared、未注册或跨 tenant reset/read 均 fail closed；
- fixture 缺失、服务不可用和 cleanup 失败留下可诊断 receipt；
- PostgreSQL + MinIO + Redis + Kubernetes 的真实测试环境验证实际执行，不能全部 skip；
- source/许可撤销后 preflight 拒绝新的 rollout。

### 5.4 提交范围

`feat(harness): bind task bundles to resettable environments`

不建设通用 sandbox、镜像编排平台或新的环境 registry 服务。

## 6. TVE-3：建立独立分层 Verifier

**状态：** `validated`（2026-08-23）。三个 versioned verifier、H5 独立判定与证据明细已实现；
9 项人工校准/reward-hacking case 均符合预期且重复判定稳定。集群 verifier 只读角色的写事务被拒绝，
并使用真实 PDF document/chunk/source lineage 完成一次 `verify_rag_outcome@1` 通过判定。定向测试
11 passed、1 个未注入数据库 URL 的集成项 skipped；同一集成项随后以真实 verifier 凭据 1 passed；
全仓回归 91 passed、38 个既有外部集成项 skipped。TVE-4 尚未开始。

**目标：** 独立判断本次试验是否有效、行为是否合规、业务结果是否成功。

### 6.1 工作项

1. 复用现有 verifier registry 和只读服务，实现：
   - `verify_environment@1`：reset/preflight/fixture/ACL/initial-state 有效；
   - `verify_task_run@1`：process、outcome、safety hard gate 与 failure taxonomy 完整；
   - `verify_rag_outcome@1`：citation 对应指定 source/page/hash，答案有证据或正确 abstain。
2. process verifier 检查 allowlisted tools、scope、预算、停止条件和失败后不得继续副作用。
3. safety verifier 检查 prompt injection、PII、越权和跨 tenant 访问；任一 hard gate 失败即任务失败。
4. quality score 只在 hard gate 通过后计算，不得覆盖 hard gate。
5. H5 evaluator 保存实际 answer、assertion details 和 evidence refs；字符串包含断言降级为配置 smoke test。
6. 使用人工标注的小型校准集和 reward-hacking fixture 检查误判；LLM judge 不单独批准成功或发布。

### 6.2 预计文件

- `src/core/verifiers.py`
- `src/harness/evaluation_runner.py`
- `src/agents/coordinator.py`
- `src/core/runtime_tools.py`
- `webui/app.py`
- `scripts/run_h5_pdf_cycle.py`
- `tests/test_verifiers.py`
- `tests/test_h5_evaluation.py`
- `tests/fixtures/verifiers/tve3_rag_calibration.json`

### 6.3 退出门禁

- verifier 使用只读/隔离凭据，尝试写被验证产物时失败；
- 对同一份保存证据重复执行得到相同结论和 contract digest；
- 环境故障稳定进入 `invalidated`，模型错误稳定进入 `failed`；
- 错误 citation、无证据断言、错误 abstain、prompt injection 和跨 tenant 读取均被拒绝；
- 人工校准结果和已知误判被记录，不能用未校准 LLM judge 关闭门禁。

### 6.4 提交范围

`feat(harness): verify rag task outcomes independently`

只实现 PDF/RAG 纵切所需 verifier；其他 task type 在有真实 bundle 后再增加。

## 7. TVE-4：真实 trial 与双模型 re-rollout

**状态：** `validated`（2026-08-23）。H5 不再预写成功 trial；每个 case 在模型调用后保存完整
transcript、Task/Environment/model/generation/verifier lineage。最小 CLI 在预注册
`dataalchemy-gpu-tve4`、真实 PostgreSQL/MinIO 和单 ROCm GPU 上，对同一 Task Bundle 分别执行
TinyLlama 与 Qwen2.5-0.5B-Instruct；两个 fingerprint 不同、两侧各 1 个有效 trial、0 invalid，独立
verifier 角色复核两个 transcript 和 gap report 均通过。两个模型都答错，gap 如实为 `failed`；这是
能力结果而非基础设施失效。CLI 会在 grounded task 的真实 RAG fixture 不可检索时于模型调用前阻断，
避免把环境缺口误记为模型失败。定向测试 20 passed、1 个未注入数据库 URL 的集成项 skipped；全仓
当时回归 96 passed、38 个既有外部集成项 skipped；真实环境 cleanup 完成。该记录是 TVE-4
关闭时的历史快照，EL-1 的当前状态见下一节。

**目标：** 证明同一 TEV 资产可以由两个不同 model fingerprint 重跑并独立比较。

### 7.1 工作项

1. 修复 H5 trial，使 campaign 为每个 case 创建真实 run/trial；禁止模型调用前写 `succeeded`。
2. evaluator 保存实际 prompt、answer、latency、model/tokenizer/template/generation fingerprint 和完整
   transcript ref/hash。
3. 新增最小 CLI `scripts/rerollout_task_bundles.py`：输入 bundle refs 和 target model config，复用
   `EvaluationService`，不建 scheduler。
4. Model A 与 Model B 使用相同 bundle、environment receipt 要求、generation policy、有效 trial 数和
   verifier policy。
5. 输出 `solved/weak/failed/invalid` gap report；`invalid` 只触发环境修复和补跑，不计能力缺口。
6. 增加 `verify_trial_transcript@1` 与 `verify_gap_report@1`。

### 7.2 预计文件

- `src/harness/evaluation.py`
- `src/harness/evaluation_runner.py`
- `src/harness/job_runner.py`
- `src/harness/jobs.py`
- `scripts/run_h5_pdf_cycle.py`
- `scripts/run_h5_rehearsal.py`
- `scripts/rerollout_task_bundles.py`
- `src/core/verifiers.py`
- `src/inference/model_manager.py`
- `tests/test_h5_evaluation.py`
- `tests/test_h5_pdf_cycle.py`

### 7.3 退出门禁

- 每个有效 trial 都有 actual answer、transcript ref/hash、Task Bundle 与真实 model fingerprint；
- 同一 bundle 由两个不同 fingerprint 完成 re-rollout；
- 两边使用相同的有效 trial、环境要求和 verifier policy；
- invalid trial 不降低 required valid trial 数，也不进入模型能力分母；
- gap report 明确逐 task 的 solved/weak/failed/invalid 和证据引用；
- 真实 PostgreSQL + MinIO + 已注册环境 + 两个允许模型实际执行，不能以 mock 关闭门禁。

### 7.4 提交范围

建议拆成：

1. `fix(harness): make evaluation trials represent real rollouts`
2. `feat(harness): rerollout task bundles across models`

## 8. EL-1：捕获并发布受治理 Experience

**前置：** TVE-4 `validated`。

**状态：** `validated`（2026-08-24）。`/api/chat` 现在由服务端创建 strict `rag_chat` task/run 并返回
权威 `run_id`；ContextService 只检索一次，Coordinator 使用同一 envelope。一个 recorder 函数将完整
可观察内容写入内容寻址 MinIO，只把 ref/hash、producer、call/retry lineage 追加到现有
`agent_events`。Agent B/C/D 与 `SFTGenerator` 暴露 model/generation/usage/latency/status 元数据，不可用
token IDs、logprobs、provider ID、tokenizer/template digest 使用 `null + reason code`，不重新推断。

真实 PostgreSQL + MinIO + ROCm 门禁完成：chat run
`a8e6a0b2-2a77-41bd-b0d2-2e5af9a9b24a` 的 conversation、model/tool trace、只读 verifier、H2 manifest
和 feedback 均绑定同一 run，所有事件对象与 manifest hash 复核通过。双模型重新 rollout 产生 gap report
`tenants/default/el1/rerollout/c6923f611b09993c37e422cf8ee33ab73a5e7c5064183f5f88963d59c7fc60f9.json`，
0 invalid；两个有效 `failed` trial 发布为 Experience
`f90e61d8dce55a428b4187e21526a508feaf7055dc9c5ada1c14ee496d3958d4`、
`bc3dcea2bc39bedbe741c9da953175293d080808c7180c4cc5909a803244a83c`，独立
`verify_experience_bundle@1` 均通过且 `training_allowed=false`。普通 chat 缺少可重置 Task Bundle/
Environment receipt 时只捕获 trace，不伪装成训练候选；`invalidated/aborted` 不发布。全仓 102 passed、
38 个既有外部集成项 skipped；另有 45 项 PostgreSQL/只读 verifier/RLS 相关集成测试实际通过。

**目标：** 保存真实 chat/model/tool 轨迹，但只有通过 TEV 有效性检查的 rollout 才成为可训练 Experience。

### 8.1 工作项

1. `/api/chat` 创建 strict `rag_chat` task/run 并返回 `run_id`；保留现有 session API。
2. `ContextService.build_context` 生成唯一权威 envelope；Coordinator 不得再次独立 retrieval。
3. 使用一个最小 recorder 将受限 request/response 写 MinIO，并向现有 `agent_events` 追加 ref/hash；
   不为每个 Agent 新建 recorder 类。
4. Agent B/C/D 和 `SFTGenerator` 记录 model revision/digest、tokenizer/template、generation config、usage、
   latency、status 和 retry；不可用 token IDs/logprobs 写稳定 reason code。
5. `experience_bundle.v1` 引用 Task Bundle、environment receipt、verifier outcome 和 H2 manifest；
   `invalidated` run 只保留诊断证据，不发布为训练候选。
6. 增加 `verify_experience_bundle@1`；Experience 发布失败不能把业务成功伪装成学习资产成功。

### 8.2 退出门禁

- 一个 chat run 可重建 `context → model call → tool → verifier → outcome`；
- 实际模型输入与保存的 context envelope/source refs 一致；
- retry 有新 call ID 和 `retry_of`，事件顺序稳定；
- secret 不进入日志、公共 manifest 或 compiler 输入；
- PostgreSQL + MinIO 集成测试实际执行，现有 RAG citation、RLS、Presidio 和反馈行为不回归。

### 8.3 提交范围

建议拆成：

1. `feat(harness): bind chat to authoritative run context`
2. `feat(harness): publish verified rollouts as experience`

## 9. EL-2：Gap analysis、Experience Compiler 与 SFT

**前置：** EL-1 `validated`。

**状态：** `validated`（2026-08-25）。已实现 `sft-success@1`、`compile_manifest.v1`、内容寻址 JSONL、
训练授权 Experience 派生、`verify_compile_manifest@1`、`verify_compile_decision@1` 与 H6 编译型训练入口
门禁；`training_snapshots` 只增加 algorithm、manifest ref/hash 和 target tokenizer/template digest，未新增表。
相同输入/config 的正向编译测试产生相同 dataset digest，holdout、solved、revoked、unapproved、重复 Task
和恢复重试均被排除；source/annotation 变化由只读 verifier fail closed。

最新一轮 DeepSeek V4 审核批准 TinyLlama 17 条、Qwen2.5 24 条模型相关 Experience，且全部保留
`human_reviewed=false`；holdout 未进入训练。TinyLlama snapshot
`80a673d8-1195-4ecf-aa6b-af3f6a70b903` 含 12 train/5 validation，Qwen2.5 snapshot
`b00ce60a-4ddc-4a73-babb-72c44b2d8d9b` 含 16 train/8 validation。两个真实 GPU Job 均训练 50 steps，
生成 candidate adapter `f8f2e4f7-a76f-4af0-9627-293f0f5d0558` 与
`cb816052-68a3-4ee5-9b64-05e20e8284df`。

**目标：** 新模型先判断能力缺口，只从需要训练且合规的 Experience 生成模型相关 SFT snapshot。

### 9.1 Gap selection

1. 复用 TVE-4 gap report；`solved` 不训练，`invalid` 修复环境后重跑；
2. `weak/failed` 经 verifier、许可和人工抽样后才成为 compiler 候选；
3. target base 已达到发布 policy 时生成 `NO-TRAIN`，不创建 snapshot/adapter。

### 9.2 `sft-success@1`

1. 只读取通过 `verify_experience_bundle@1`、`training_allowed=true` 且未撤销的 Experience；
2. 排除 holdout、invalid、跨 tenant、新 base 已 solved 和未经批准的数据；
3. 按事件依赖提取完成任务所需成功路径，不把失败重试教成期望行为；
4. 使用 target tokenizer/chat template 编译，输出 JSONL 与 `compile_manifest.v1`；
5. 复用 H5 `training_snapshots`、审批、Job、adapter 和撤销链；不新增 compiled-dataset 表。

### 9.3 最小迁移

只有现有 JSONB/ref 不足时，才在 `training_snapshots` 增加 algorithm、compile manifest key/hash、
target tokenizer 和 chat template digest。`base_model_digest` 继续表示 target base。

### 9.4 退出门禁

- `solved/invalid/holdout/revoked/unapproved` 不进入 compiled dataset；
- 任一 item 可追溯到 Experience、label、compiler config 和 target fingerprint；
- 相同输入/config 重编译得到相同 dataset digest；
- source 删除、修改或撤销使 compiler/verifier fail closed；
- 训练入口缺少 verified compile manifest 时拒绝执行。

## 10. EL-3：跨模型受控 A/B

**状态：** `engineering-go`（2026-08-28）。v2 的原 policy NO-GO 保留不变；v3
train/validation/holdout 为 150/44/100，共 294 个 Bundle。validation 选择 TinyLlama adapter 后，
三次冻结 holdout 为 base 38/37/37、candidate 98/98/98、0 invalid；critical 与 p95 门禁均通过。
Qwen2.5 validation 4/44，未进入 holdout。

失败归因已完成：v2 89/100 候选的 11 个 holdout 失败全部为
`rag_answer_assertion_failed`，引用证据存在，属于答案生成/截断问题；归因产物为
`tenants/default/annotations/holdout-failure-analysis/9198c33204fbf81b62e9095910c16e00ba67f352f7bf976275f1d67d7bef9d8a.json`。
后续 v2 候选的 13 个失败同类；这些是 v3 改进前的历史诊断，不是最终 98/100 候选的失败数。

**目标：** 比较新 base、gap-only SFT 和可选全量 SFT，决定是否训练和发布。

对同一冻结 Task Bundle 集运行：

- A：新 base，不训练；
- B：新 base + gap-only SFT；
- C：可选全量 approved SFT，仅作研究对照。

三组固定 model/tokenizer、bundle、environment、generation config、trial 数和 verifier policy。记录任务
成功、安全 hard gate、训练 token/time、推理成本/p95 和人工抽样。

决策：A 达标为 `NO-TRAIN`；B 不优于 A 或 regression 退化为 `NO-GO`；环境、许可或校准不足为
`BLOCKED`；只有 B 达标且收益覆盖成本才允许继续 gap-only 路线。

退出门禁：报告必须包含真实 fingerprint、source/compile hash、有效 trial 对齐、invalid 与模型失败分开
统计，并给出 `GO/NO-GO/NO-TRAIN/BLOCKED`。

### 10.1 EL-3R：发布证据修复与 model-specific SFT 重训

**状态：** `completed / engineering-go`（2026-08-28）。EL-3R-0--EL-3R-5 已落地；三轮使用同一
candidate、context cache、generation policy 和 verifier，decision 经独立重放。adapter 已绑定 decision
并 verified，本地 release 已完成 shadow、300-sample offline canary 和 promoted。

**目标：** 先使发布结论只依赖可验证 holdout、critical hard gate 和不可变成本，再分别修正 TinyLlama
与 Qwen 的数据和训练；不通过降低 verifier 或临时放宽 policy 制造 `GO`。

实施顺序固定为：

```text
EL-3R-0 契约冻结
  -> EL-3R-1 split-aware migration / hard gate 分离
  -> EL-3R-2 Training Cost Receipt
  -> EL-3R-3 model-specific Experience 再编译
  -> EL-3R-4 completion-only SFT / validation 选模
  -> EL-3R-5 三次受控 A/B 与迁移决策
```

#### EL-3R-0：契约冻结

1. 冻结 `migration_report.v2`、`training_cost_receipt.v1` 与兼容策略；旧 report 保持只读可验证；
2. 冻结 release suite：train 只诊断、validation 只选模、holdout 只发布、critical 单独 hard gate；
3. 冻结产品阈值、最小样本数、重复次数、延迟和成本单位；阈值变更必须产生新 policy version；
4. 增加失败 fixture：split 混算、holdout 泄漏、成本裸值、critical 失败、base/candidate 不对齐均拒绝。

退出门禁：契约测试先失败后通过，旧 `migration_report.v1` 证据仍可读取但不能冒充 v2 发布证据。

#### EL-3R-1：split-aware migration 与 hard gate 分离

1. Migration arm 按 train/validation/holdout/critical 保存 required、valid、invalid、success、pass rate、P95；
2. release capability/improvement 只读取 holdout，不把 train 的记忆收益计入发布；
3. hard gate 只覆盖证据有效性、ACL/许可、安全、critical suite 和 invalid trial；普通任务失败进入
   capability，不再以 `succeeded == valid` 重复表达 `min_pass_rate`；
4. base/candidate 继续强制对齐 Task Bundle、initial-state、generation policy 和 verifier contract。

退出门禁：构造“train 提升、holdout 持平”的 fixture 必须 `NO-GO`；critical 失败必须 fail closed；相同
report 重放得到相同 decision。

#### EL-3R-2：不可变训练成本

1. 训练 Job 发布内容寻址成本回执：snapshot/adapter/model/dataset digest、时间、GPU、GPU seconds、steps、
   processed tokens、peak VRAM、normalized cost、metrics ref 与 policy version；
2. Adapter Manifest 引用 receipt；独立只读 verifier 从 Job/metrics 证据重算 hash 和标准化成本；
3. Migration candidate 只接受 verified receipt 派生的成本，拒绝 CLI 裸浮点数、缺单位和未知值；
4. 为 gfx1151 单 GPU 冻结第一版归一化单位和 `max_training_cost`，不把 wall time 直接冒充经济成本。

退出门禁：缺失、篡改、跨 snapshot、单位未知或超过预算均阻断；合法回执可从 adapter 追溯并独立复算。

#### EL-3R-3：按模型重建训练资产

1. 在冻结 Task Bundle 上分别重跑 TinyLlama/Qwen base，只选择各自 weak/failed gap；
2. DeepSeek 只生成候选期望回答/成功路径；必须在原 Environment 执行且由冻结 verifier 通过后才授权；
3. compiler 排除 solved、invalid、holdout、重复/近重复 task、失败探索和未经批准数据；
4. 扩充文档隔离任务，先以学习曲线逐步增加 train/validation；holdout 至少 100 条且在最终 A/B 前冻结；
5. 保留 `human_reviewed=false` 标识，生产推进仍需人工校准。

退出门禁：任一 compiled item 可追溯到目标模型 gap 和 verified success；无 holdout 泄漏；任务增量停止由
学习曲线决定，不以固定样本数宣称充分。

#### EL-3R-4：修正 SFT

1. assistant completion token 计算 loss，system/user/tool prompt label 设为 `-100`；
2. validation 真正执行 evaluation 并选择 checkpoint，保存 loss、task success、seed 与超参数；
3. TinyLlama 只做最小 learning-rate/rank/epoch 比较；Qwen 先通过 5--10 条高质量样本过拟合诊断，失败时
   检查 chat template、EOS/PAD、label mask、target modules 和 adapter 加载，不开始全量训练；
4. 每个候选产生新的 snapshot、adapter、fingerprint、cost receipt，不覆盖旧 artifact。

退出门禁：completion mask 单测可解码核对；validation 参与 checkpoint 选择；Qwen 最小诊断通过后才允许
全量 Job；所有训练参数进入不可变上下文。

#### EL-3R-5：重新 A/B 与发布

1. 冻结 verifier、holdout、generation policy 和 base/candidate fingerprint；
2. 至少三次独立 rollout，保存每次 split 指标与聚合结论；
3. 要求 0 invalid、critical/safety 100%、holdout 达到冻结 pass-rate/improvement policy、P95 不超过
   base × 1.20、成本不超预算；
4. TinyLlama/Qwen 分别决策；一个模型失败不得由另一个模型结果覆盖；
5. 只有 `GO` 才把 adapter 从 candidate 推进 verified/release；否则 revoke 或保留实验 candidate。

退出门禁：migration report 与 decision 经独立 verifier 重放；Tiny holdout 仅持平和 Qwen 质量退化的
fixture 均不得发布。EL-3R 通过后才重新执行 EL-4/EL-5 条件决策。

#### 实际文件与完成边界

优先复用现有对象、JSONB 和 evidence store，不预先新增表或依赖：

| 提交 | 主要文件 | 完成边界 |
| --- | --- | --- |
| EL-3R-0/1 | `src/harness/model_migration.py`、`scripts/decide_gap_ab.py`、`tests/test_model_migration.py` | v2 split 指标、hard gate/capability 分离、v1 兼容与独立重放。 |
| EL-3R-2 | `src/train.py`、`src/harness/job_runner.py`、现有 adapter/evidence persistence、相关 verifier/tests | 成本回执发布、adapter 引用、只读复算；仅当现有 JSONB/ref 无法安全表达时增加最小迁移。 |
| EL-3R-3 | `src/harness/compiler.py`、审核/编译脚本、`tests/test_compiler.py` | target-specific verified-success selection、去重与 holdout 隔离。 |
| EL-3R-4 | `src/train.py`、训练上下文 validator、`tests/test_pdf_training_candidates.py` | completion mask、validation 选模、参数/seed lineage 与 Qwen 最小诊断。 |
| EL-3R-5 | `scripts/rerollout_task_bundles.py`、`scripts/evaluate_repeated_release.py`、`scripts/promote_tiered_release.py`、发布治理与证据文档 | 三次真实 A/B、独立 GO decision、adapter verified 与 engineering promoted；运行数据不混入源码提交。 |
| Policy/diagnosis | `src/harness/release_policy.py`、`scripts/analyze_holdout_failures.py`、`verify_release_decision@1`、对应 tests | critical 100%、普通能力分层阈值、三次重复契约、内容寻址失败归因与独立决策重放。 |

每个提交只关闭自己的单元/集成门禁；需要 PostgreSQL、MinIO、GPU 或真实模型的验证不能由 mock 替代，
也不能与下一工作包合并成一个不可定位失败的大提交。

## 11. EL-4：DPO 条件决策

**状态：** `not-enabled`（2026-08-28）。v3 SFT candidate 已达到当前 synthetic release policy，
没有“经校准 preference pair 能解决的剩余发布缺口”，也没有生产人工校准与 preference-training 许可；
因此未创建 DPO compiler、trainer、表或依赖。v2 的旧 decision digest 仅保留为历史证据。

只有同时满足以下条件才实现 DPO compiler/trainer：

- 有足量同 Task Bundle、同 environment、同 decision point 的可比较 good/bad pairs；
- preference 来自校准 verifier 或人工 review；
- SFT 已验证但仍存在明确质量缺口；
- 数据许可明确允许 preference training。

不满足时不创建 DPO 抽象、数据库表或新依赖。

## 12. EL-5：RL 与 Agent Lightning 条件决策

**状态：** `not-enabled`（2026-08-28）。SFT 已达到当前 synthetic policy，且尚无生产级批量 reset、
校准 reward/reward-hacking、token/logprob telemetry、RL 预算或“SFT/DPO 无法达标”的证据；因此
Agent Lightning 为 `NOT-SELECTED / rl_not_enabled`。v2 的旧 decision digest 仅保留为历史证据。

只有 environment 可稳定批量 reset、reward/reward-hacking 已校准、token IDs/logprobs 语义可验证、训练
预算获批，且 SFT/DPO 无法达到目标时，才评估 RL。

若条件满足，只做隔离 PoC：DataAlchemy 输出 Task Bundle，Agent Lightning 临时承担 rollout/training，
Experience/Compile Manifest 回写 DataAlchemy；PostgreSQL/MinIO 继续是许可、资产和发布权威。PoC 不能
证明收益或保持单一权威时删除集成。

## 13. 测试与验证矩阵

| 层级 | 最小验证 | 不能证明 |
| --- | --- | --- |
| Unit | schema/hash、状态分类、secret/hidden-answer rejection。 | 环境可重置、RLS、真实模型语义。 |
| Environment integration | reset/preflight/initial-state、隔离和 cleanup receipt。 | 模型任务能力。 |
| PostgreSQL + MinIO | bundle/receipt/evidence publish、hash、删除和部分失败恢复。 | 跨模型收益。 |
| Verifier calibration | PDF/RAG outcome、安全 hard gate、人工校准和 reward hacking。 | 代表性生产价值。 |
| Dual-model replay | 同 bundle、同环境、同 policy 的两个真实 fingerprint。 | 训练收益。 |
| Controlled A/B | base/gap-only/full 的能力、回归和成本。 | H6 真实团队试点与 GA。 |

每个工作包运行与其范围相符的最小测试及 `git diff --check`。涉及 PostgreSQL、MinIO、reset、模型或
GPU 的门禁若因环境缺失而 skip，报告必须列出 skip，状态不得标记为 `validated`。

## 14. 状态模板

| 状态 | 含义 |
| --- | --- |
| `planned` | 只有设计或排期。 |
| `implemented` | 实现完成，但真实退出门禁未全部运行。 |
| `validated` | 该工作包全部必需门禁在指定环境通过。 |
| `blocked` | 外部环境、数据许可或模型资源阻塞。 |
| `no-go` | 实测收益或安全不满足，停止该路线。 |
| `engineering-go` | 公共 synthetic 工程门禁与本地发布状态机通过，不代表生产资格。 |
| `not-enabled` | 条件能力尚未获准开始。 |

工作包只有运行自己的真实退出门禁才能超过 `implemented`；当前 TVE/EL 已有 reset、GPU 训练、三次
A/B 和独立 verifier 证据，因此可标记 engineering GO。H6 外部试点仍需自己的生产代表性证据。

## 15. 下一工作包

当前不再有未实现的 EL-3R 代码包。TVE-0--TVE-4、EL-1--EL-5 的公共 synthetic 计划链已执行到
可复核终态：EL-3 为 `engineering-go`，EL-4/EL-5 为 `not-enabled`。当前证据如下：

1. 已完成：固定官方 MultiDoc2Dial revision/source hash/license；v3 release suite 为 train 150、
   validation 44、evaluation_holdout 100，共 294 个 Task Bundle；v2 378-task suite 作为历史基线保留；
2. 已完成：三个独立环境 reset/preflight 均 ready；TinyLlama 完成 v3 validation 与三次
   holdout A/B，Qwen2.5 完成 validation 诊断后按停止门禁未进入 holdout；
3. 已完成：DeepSeek V4 审核失败 gap，labels 保留 `human_reviewed=false`，holdout 未进入训练；
4. 已完成：TinyLlama completion-only SFT、GPU training cost receipt、validation 选模与三次 holdout A/B；
   candidate 98/98/98，base 38/37/37；
5. 已完成：release decision `18713148…dab9` 经独立 verifier 复核为 GO，adapter verified，engineering
   release `5c974571…08fb` promoted；
6. 生产推进仍需人工校准、代表性任务和真实流量 shadow/canary；EL-5 还需单独验证批量 reset、
   reward/reward-hacking、token telemetry 与预算，全部通过才允许
   Agent Lightning 隔离 PoC。

公共 fixture 只解除“没有可复现 Task 数据”的阻塞，不自动授予训练许可，也不把 synthetic/unit 验证
升级为真实模型收益证据。
