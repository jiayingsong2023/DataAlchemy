# Task-Environment-Verifier-first Agent Learning 实施计划

> 状态：EL-5 条件决策已完成；RL 为 `not-enabled`，Agent Lightning 为 `not-selected`。代码基线：
> `feat/harness-tve`（2026-08-24）。
> 公共 MultiDoc2Dial 40-case replay fixture 已完成入库、三环境 reset/preflight、Task Bundle/receipt 发布和
> TinyLlama/Qwen2.5 双模型 re-rollout；尚未据此产生已审核 train/validation Experience 或重跑 EL-2/EL-3。
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

**状态：** `validated`（2026-08-24）。已实现 `sft-success@1`、`compile_manifest.v1`、内容寻址 JSONL、
训练授权 Experience 派生、`verify_compile_manifest@1`、`verify_compile_decision@1` 与 H6 编译型训练入口
门禁；`training_snapshots` 只增加 algorithm、manifest ref/hash 和 target tokenizer/template digest，未新增表。
相同输入/config 的正向编译测试产生相同 dataset digest，holdout、solved、revoked、unapproved、重复 Task
和恢复重试均被排除；source/annotation 变化由只读 verifier fail closed。

真实 PostgreSQL + MinIO 门禁复用 EL-1 gap report 与两份 Experience。两条来源均为
`evaluation_holdout`，所以系统两次得到同一 `NO-TRAIN` decision
`e3432048b167ce10d8de77d193a57632561b104999d79fc404af113239b2736a`，eligible=0，独立 verifier 通过，
且 `sft-success@1` snapshot/adapter 数均为 0。该结论禁止用 holdout 或错误答案伪造训练成功；正向编译
能力由自动化契约测试证明，不冒充真实训练收益。全仓 110 passed、38 skipped；PostgreSQL 集成复跑
63 passed。

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

**状态：** `blocked`（2026-08-24）。`model_migration_report.v1`、四态 policy、内容寻址发布和
`verify_model_migration@1` 已实现；真实 PostgreSQL + MinIO 连续两次产生相同报告 digest
`71fcd49a7ecfbab46a579fca78b54e39d4da5f6ba633bf917abd11a79bb32156`，独立复核通过。EL-2 没有合格
非 holdout train/validation 来源，因此没有 snapshot、adapter 或 candidate arm；不得伪造受控 A/B，
终局为 `BLOCKED / candidate_unavailable`。

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

## 11. EL-4：DPO 条件决策

**状态：** `not-enabled`（2026-08-24）。已实现 `dpo_gate_decision.v1`、内容寻址发布和
`verify_dpo_gate@1`；真实 PostgreSQL + MinIO 连续两次产生相同 digest
`e5c76888412498e3c82f478c0c38a73261cf0ae1879ca6498d1c894dda08af96`，独立 verifier 从 EL-3 向上重放
EL-2/TVE-4 证据后通过。结论为 `NOT-ENABLED / sft_not_validated`，未创建 DPO compiler、trainer、表或
依赖。

只有同时满足以下条件才实现 DPO compiler/trainer：

- 有足量同 Task Bundle、同 environment、同 decision point 的可比较 good/bad pairs；
- preference 来自校准 verifier 或人工 review；
- SFT 已验证但仍存在明确质量缺口；
- 数据许可明确允许 preference training。

不满足时不创建 DPO 抽象、数据库表或新依赖。

## 12. EL-5：RL 与 Agent Lightning 条件决策

**状态：** `not-enabled`（2026-08-24）。已实现 `rl_gate_decision.v1`、内容寻址发布和
`verify_rl_gate@1`；真实 PostgreSQL + MinIO 连续两次产生相同 digest
`e350f00ab09c6a122a1942178512cb0b917bc122e7bc9441abb756838ac6483f`，独立 verifier 从 EL-4 向上重放
EL-3/EL-2/TVE-4 证据后通过。结论为 `NOT-ENABLED / upstream_learning_gates_not_satisfied`，Agent
Lightning 为 `NOT-SELECTED / rl_not_enabled`；未实现 RL、安装依赖或创建第二套控制面。

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
| `not-enabled` | 条件能力尚未获准开始。 |

文档、单元测试和 synthetic fixture 最多推进到 `implemented`；TVE-2 的真实 reset、TVE-4 的双模型
re-rollout、EL-2 的编译训练、EL-3 的受控 A/B 和 H6 外部试点分别需要自己的真实证据。

## 15. 下一工作包

TVE-0--TVE-4、EL-1--EL-5 的当前计划链已执行完毕；最终状态不是 RL 完成，而是可复核地停止在
EL-3 `blocked`、EL-4/EL-5 `not-enabled`。不新增学习工作包，按现有链补齐真实证据：

1. 已完成：固定官方 MultiDoc2Dial revision/source hash/license，生成文档隔离的 train 20、validation 8、
   evaluation_holdout 12 三套 PDF/RAG suite；Task Bundle 发布入口已保留并验证 case split；
2. 已完成：三个独立环境 reset/preflight 均 ready，40 个 Task Bundle/receipt 已发布；TinyLlama 与
   Qwen2.5 共完成 80 个真实 trial，gap report `62b326f0ba6431548b3467a6651828d97db3c75ef7c0d689585e0169b837e14a`
   经独立 verifier 角色复核通过，40 valid、0 invalid、solved 4、weak 17、failed 19；
3. 待执行：人工复核 weak/failed，只有期望答案、许可和 source lineage 完整的 train/validation trial
   才发布为 Experience；holdout 永不进入训练；
4. 待执行：重跑 EL-2，只有 gap-only compiler 产生 verified snapshot 后才训练 adapter；随后重跑 EL-3
   受控 A/B，并根据结果重新评估 EL-4；
5. EL-5 仍需单独验证批量 reset、reward/reward-hacking、token telemetry 与预算，全部通过才允许
   Agent Lightning 隔离 PoC。

公共 fixture 只解除“没有可复现 Task 数据”的阻塞，不自动授予训练许可，也不把 synthetic/unit 验证
升级为真实模型收益证据。
