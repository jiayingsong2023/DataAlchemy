# WebUI Run 聚合与反馈训练索引设计

> 状态：待实施设计。本文覆盖两个待办：
>
> 1. WebUI 将 Memory、LoRA、评测和发布全部串入同一动态 `run_id`；
> 2. WebUI 用户反馈自动进入 PostgreSQL 训练候选权威索引。
>
> 本设计不依赖 H5 canonical registry-clean 镜像，也不把 synthetic rehearsal 视为真实业务
> 质量验收。实现完成后仍需用真实代表性数据完成 H6 资格门禁。

## 1. 当前状态与问题边界

当前已经具备：

- `GET /api/runs/{run_id}` 和 `_run_details()` 基础 run 页面；
- strict task、evidence manifest、verifier、checkpoint 和 tenant RLS；
- Memory distillation、Memory Governance 和 `/api/memories`；
- H5 的 `trajectory_annotations`、training snapshot、evaluation、adapter、release 表和 API；
- 反馈的 MinIO 保存、反馈更新和审核 API。

当前缺口：

1. `_run_details()` 的 Memory、training candidate、LoRA、evaluation、release 仍有固定的
   `waiting_for_input`/`blocked_by_phase` 占位状态，没有查询真实治理表；
2. `evaluation_campaigns`、`training_snapshots`、`adapter_manifests` 和 `release_records`
   没有统一的 run 聚合关系；一个 snapshot 还可能来自多个 run；
3. `/api/feedback` 仍以 MinIO 文件为主要状态来源，没有自动创建 H5
   `trajectory_annotations(kind=user_feedback)`；
4. 反馈审核结果无法被训练候选查询可靠、幂等地消费；MinIO 与 PostgreSQL 失败时也没有统一
   reconciliation 语义。

## 2. 设计原则

- **PostgreSQL 是状态权威，MinIO 是不可变内容证据**：反馈正文、transcript 和 manifest
  可以保存在 MinIO，但状态、审核、许可和候选资格必须在 PostgreSQL；
- **同一个 `run_id` 不等于一条训练样本**：run 聚合展示来源关系，训练 snapshot 仍可从多个
  run 选择已批准 annotation；页面必须显示 `source_run_ids`，不能伪造单一来源；
- **状态由真实记录计算**：禁止用固定 `blocked_by_phase` 覆盖数据库中的候选、审批、评测或
  发布状态；未知、缺证据和跨 tenant 关系统一显示 `blocked`；
- **反馈先落证据，再进入权威索引**：MinIO 对象 hash 校验成功且 PostgreSQL annotation
  成功后，反馈才可进入训练候选查询；任一环节不确定时保持 `pending_reconciliation`；
- **失败不放大权限**：所有 run 聚合、反馈、annotation、snapshot 查询继续使用当前 tenant
  RLS 和最小角色；
- **幂等优先**：同一 `feedback_id`、同一 run 和同一内容 hash 重试不得产生重复 annotation
  或重复训练候选。

## 3. 统一 Run Gate 关系

### 3.1 新增迁移

新增 `016_harness_run_gate_links.sql`，建立通用关系表：

```sql
CREATE TABLE run_gate_links (
    link_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES agent_tasks(run_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    gate_name TEXT NOT NULL CHECK (gate_name IN (
        'memory', 'feedback', 'training_candidate', 'lora', 'evaluation', 'release'
    )),
    entity_type TEXT NOT NULL CHECK (entity_type IN (
        'memory', 'trajectory_annotation', 'training_snapshot',
        'evaluation_campaign', 'adapter_manifest', 'release_record'
    )),
    entity_id UUID NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN (
        'source', 'produced', 'evaluated', 'released', 'blocked_by'
    )),
    state TEXT NOT NULL CHECK (state IN (
        'pending', 'candidate', 'approved', 'running', 'passed', 'failed',
        'blocked', 'revoked', 'expired'
    )),
    evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, gate_name, entity_type, entity_id, relation)
);
```

要求：

- `tenant_id` 必须与 `agent_tasks` 和实体表的 tenant 一致；写入时由数据库/服务校验；
- `run_id` 只引用已经存在的 `agent_tasks.run_id`；
- 所有新表启用 `ENABLE ROW LEVEL SECURITY` 和 `FORCE ROW LEVEL SECURITY`；
- app role 可按 tenant 读写，verifier role 只读；
- `entity_id` 的存在性由 link service 在写入时检查，verifier 在读取时再次检查；
- 不使用软删除覆盖已发布关系；撤销、删除和过期写入新的状态和 evidence。

### 3.2 关系生成规则

| 关系 | 触发点 | 示例 |
| --- | --- | --- |
| `memory / source` | distillation 产生带来源事件的候选 | `run_id → memory_id` |
| `feedback / source` | 用户提交带 run 的 feedback | `run_id → annotation_id` |
| `training_candidate / produced` | annotation 通过审核且允许训练 | `run_id → annotation_id` |
| `training_candidate / source` | snapshot 包含 annotation | 每个 source annotation 建 link |
| `lora / produced` | lora Job 产生 adapter manifest | source run → adapter |
| `evaluation / evaluated` | evaluation campaign 绑定 base/adapter | source run → evaluation |
| `release / released` | release governance 晋级/回滚 | source run → release |
| `gate / blocked_by` | 缺少审批、证据、资格或跨 tenant 关系 | 记录阻塞原因和 policy version |

snapshot、evaluation 和 release 可以关联多个 `source_run_ids`。页面显示聚合关系，而不是把
其中一个 run 错当成唯一来源。

### 3.3 Gate 状态计算

`RunGateService` 只读聚合以下来源：

```text
agent_tasks / agent_events / verifications / run_manifests
trajectory_annotations / training_snapshots / evaluation_campaigns
adapter_manifests / release_records / memories / run_gate_links
```

每个 gate 返回：

```json
{
  "name": "release",
  "state": "blocked",
  "reason": "candidate_evaluation_missing",
  "entity_ids": [],
  "source_run_ids": ["..."],
  "evidence": [],
  "policy_version": "h5-release.v1",
  "updated_at": "..."
}
```

状态优先级固定为：

```text
revoked/expired > failed > blocked > running > approved/passed > candidate > waiting_for_input
```

只要存在跨 tenant、ACL 不匹配、缺 hash、缺 verifier 或证据 pending，状态不得为 `passed`。

## 4. 反馈进入训练候选权威索引

### 4.1 Feedback 数据模型

扩展 `trajectory_annotations`，增加稳定的用户反馈幂等标识：

```sql
ALTER TABLE trajectory_annotations
    ADD COLUMN source_feedback_id TEXT,
    ADD COLUMN feedback_rating TEXT,
    ADD COLUMN feedback_comment TEXT;

CREATE UNIQUE INDEX trajectory_annotations_feedback_id_idx
    ON trajectory_annotations (tenant_id, source_feedback_id)
    WHERE source_feedback_id IS NOT NULL;
```

`source_feedback_id` 由 WebUI 首次创建反馈时生成；MinIO object key 和 PostgreSQL annotation
都使用它。历史只有 MinIO 的反馈标记为 `legacy_unindexed`，不得自动进入训练候选，除非重新
绑定 tenant、run、ACL 和 reviewer。

### 4.2 提交流程

```text
WebUI feedback(run_id, rating, comment)
  → 验证 run 属于当前 tenant 且允许反馈
  → 生成 source_feedback_id 和 canonical JSON
  → MinIO 写入 content-addressed object
  → 读回并验证 SHA-256
  → PostgreSQL INSERT trajectory_annotations(kind=user_feedback,
       status=unrated, training_allowed=false, run_id, source_acl_digest)
  → INSERT run_gate_links(gate=feedback, relation=source)
  → 返回 feedback_id + annotation_id + state=unrated
```

反馈文本只能作为 untrusted content。它不能修改 TaskSpec、tool scope、tenant、memory policy
或 release policy。

### 4.3 审核流程

```text
reviewer/admin review
  → 读取 MinIO 原文和 SHA-256
  → 检查 run、source ACL、tenant、prompt injection 和敏感信息
  → rejected/revoked：training_allowed=false
  → approved：必须同时写 training_purpose、permission_version、reviewer、reviewed_at
  → run_gate_links 更新 training_candidate 状态
```

只有以下谓词同时为真，候选才能被 snapshot builder 读取：

```text
annotation.kind = user_feedback
annotation.status = approved
annotation.training_allowed = true
annotation.source_feedback_id is not null
annotation.content_sha256 matches MinIO read-back hash
annotation.source_acl_digest matches original run ACL
annotation.tenant_id = current tenant
```

撤销反馈、删除源文档、ACL 收紧、tenant 变更或 permission version 失效时，必须传播为
`revoked`，并阻止新 snapshot；已经发布的 adapter 由 H5/H6 治理决定是否撤销。

### 4.4 失败与恢复

| 情况 | 状态与处理 |
| --- | --- |
| MinIO 写入失败 | 不创建 annotation，返回可重试错误 |
| MinIO 写成功、DB 写失败 | 保留 orphan evidence，写 reconciliation 任务，不允许训练读取 |
| DB 成功、link 写失败 | 事务回滚 annotation；不返回成功 |
| 重复 feedback request | 根据 `(tenant_id, source_feedback_id)` 返回原 annotation |
| reviewer 请求超时 | 保持 `unrated`，不进入候选 |
| hash 不匹配 | `rejected`/`blocked`，保留 evidence 和失败原因 |
| 跨 tenant run | 404/403 fail closed，不泄露 run 是否存在 |

## 5. WebUI API 设计

### 5.1 Run 聚合

保留现有接口：

```http
GET /api/runs/{run_id}
GET /api/runs/{run_id}/manifest
```

将 `_run_details()` 的固定 `future_gates` 替换为：

```python
gates = RunGateService(DATABASE_URL).summarize(run_id, identity)
```

新增可选明细接口：

```http
GET /api/runs/{run_id}/gates
GET /api/runs/{run_id}/memory
GET /api/runs/{run_id}/feedback
GET /api/runs/{run_id}/learning
```

`/learning` 一次返回 annotation、snapshot、evaluation、adapter、release 的关联和状态；
所有返回结果都必须带 entity hash/ID、source run、tenant 和 evidence 引用。

### 5.2 Feedback

保留兼容接口，但改变权威写入：

```http
POST /api/feedback
{
  "run_id": "...",
  "rating": "good|bad",
  "comment": "...",
  "expected_version": 3,
  "client_request_id": "..."
}
```

返回：

```json
{
  "feedback_id": "...",
  "annotation_id": "...",
  "run_id": "...",
  "state": "unrated",
  "training_eligible": false
}
```

审核接口继续使用 maker-checker：

```http
POST /api/feedback/{feedback_id}/review
POST /api/annotations/{annotation_id}/decision
```

普通用户只能提交反馈和查看自己的状态；reviewer/admin 才能审核。创建者不能审核自己的
反馈作为训练样本。

## 6. WebUI 页面设计

### 6.1 Run 时间线

页面以一条纵向 timeline 展示：

```text
Input
  → Rough clean
  → Refine
  → Publish / RAG
  → Feedback
  → Memory
  → Training candidate
  → Snapshot
  → LoRA
  → Evaluation
  → Shadow/Canary
  → Release/Rollback
```

每个阶段展示：

- 状态、更新时间和阻塞原因；
- source/entity ID 和 SHA-256；
- tenant/ACL 摘要；
- verifier 名称、版本和结论；
- MinIO evidence、Kubernetes Job 和日志链接；
- 审批人、审批时间和 policy version；
- 关联的其他 `source_run_ids`。

### 6.2 状态显示规则

- `waiting_for_input`：等待用户反馈、问题或人工操作；
- `candidate`：已形成候选但未批准；
- `approved`：审核通过，但不等于训练/发布成功；
- `running`：Job 或评测正在执行；
- `passed`：独立 verifier 和 hard gate 都通过；
- `blocked`：缺资格、证据、审批、ACL 或依赖；
- `failed`：执行或独立验证失败；
- `revoked/expired`：来源或权限已撤销/过期。

页面不得把“存在 adapter”显示成“已发布”，不得把“用户反馈 good”显示成“训练合格”。

## 7. 实施顺序

### F1：反馈索引与幂等

1. 新增迁移 016：feedback columns、唯一幂等键、RLS 和 run gate link；
2. 抽取 `FeedbackIndexService`，统一 MinIO evidence、hash 和 PostgreSQL annotation；
3. 改造 `/api/feedback` 和审核 API；
4. 为旧 MinIO feedback 增加 `legacy_unindexed` 查询标记；
5. 增加失败恢复、重复提交和跨 tenant 测试。

### F2：Run gate 聚合

1. 实现 `RunGateService` 和只读 SQL 查询；
2. 在 memory distillation、annotation、snapshot、evaluation、adapter、release 创建/状态
   变更处写入 link；
3. 替换 `_run_details()` 的固定 gate 列表；
4. 增加 `/api/runs/{run_id}/gates` 和 `/learning`；
5. 增加 UI 时间线和 entity 详情展开。

### F3：端到端验收

使用隔离 PostgreSQL、MinIO 和 synthetic run 完成：

```text
create strict run
  → RAG answer
  → submit feedback
  → reviewer approve
  → annotation eligible
  → snapshot source visible
  → evaluation/release state visible in same run
```

## 8. 验收测试

### 必须通过

- [ ] 一个 run 的 WebUI timeline 显示 Memory、feedback、training candidate、LoRA、evaluation、
  release 的真实状态，不再使用固定 gate 占位；
- [ ] 用户 feedback 自动创建 PostgreSQL annotation，并保留 MinIO hash/证据；
- [ ] 重复请求不会创建重复 annotation；
- [ ] 未审核、未授权、ACL 不匹配和跨 tenant feedback 不能进入 snapshot；
- [ ] feedback、annotation、snapshot、evaluation、adapter、release 都能回溯到 source run；
- [ ] MinIO/DB 部分失败可 reconciliation，不能产生“可训练但无证据”的记录；
- [ ] 撤销反馈或来源 ACL 后，候选资格正确收紧；
- [ ] reviewer maker-checker、RLS 和普通用户权限测试通过；
- [ ] WebUI API/UI 测试覆盖 `candidate → approved → rejected/revoked → blocked/passed`。

### 不在本任务退出门禁内

- 真实业务数据代表性和人工校准质量；
- H5 canonical registry-clean 镜像；
- 两支真实团队四周 GA-01 试点；
- 自动生成高质量训练标签或 LLM judge 的生产资格。

## 9. 交付物

```text
016_harness_run_gate_links.sql
FeedbackIndexService
RunGateService
更新后的 /api/feedback、/api/runs/{run_id} 和 WebUI timeline
迁移/RLS/幂等/失败恢复/跨 tenant 测试
一份 synthetic run 聚合和 feedback-to-annotation evidence manifest
```

完成本设计后，`docs/TODO.md` 中以下两项才可标记 `[x]`：

```text
产品完整闭环展示
在线质量闭环
```

真实数据、人工校准和外部试点相关待办仍保持未完成。
