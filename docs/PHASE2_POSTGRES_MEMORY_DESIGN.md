# Phase 2 详细设计：PostgreSQL + pgvector 统一记忆与检索底座

**状态：** 已确认设计，待实施  
**分支：** `feat/phase-2-memory-governance`  
**范围：** Phase 2（第 11～18 周）

## 1. 决策与边界

本阶段直接以 PostgreSQL + pgvector 替换当前的 FAISS、SQLite 元数据和
`rank_bm25` 索引。项目不需要兼容旧索引、旧数据或在线平滑迁移；因此不做
双写、影子检索或两套向量库并存。

保留 CrossEncoder 作为最终精排器。Redis 继续提供 TTL 工作状态、缓存、锁和
队列，不保存长期事实。MinIO 继续保存原始文档、上传文件和不可变产物；它不是
检索数据库。

不在本阶段引入图数据库、第三方记忆框架、第二个向量数据库或多智能体。

## 2. 目标架构

```text
                 ┌───────────────────────────────────┐
上传/连接器 ───→ │ MinIO：原始文件、版本化产物         │
                 └──────────────┬────────────────────┘
                                │ 解析、切块、嵌入
                                ▼
┌───────────────┐       ┌─────────────────────────────────────────┐
│ Agent Runtime │──────→│ PostgreSQL + pgvector                    │
│ 任务/工具/审批 │       │ events / checkpoints / documents / chunks│
└───────┬───────┘       │ memories / ACL / versions / deletion log │
        │               │ RLS + GIN 全文索引 + HNSW 向量索引       │
        │               └────────────────┬────────────────────────┘
        │                                │
        ▼                                ▼
┌──────────────────┐            ┌──────────────────────┐
│ Redis             │            │ Retriever            │
│ TTL 工作记忆/缓存 │            │ FTS + vector → RRF   │
│ 锁/队列           │            │ → CrossEncoder       │
└──────────────────┘            └──────────┬───────────┘
                                             ▼
                                      有来源的 Prompt 上下文
```

## 3. 数据职责

| 数据 | 权威位置 | 说明 |
| --- | --- | --- |
| 原始文档、数据集、模型和运行产物 | MinIO | 不可变对象，以哈希和版本标识。 |
| 文档、chunk、向量、全文索引与引用 | PostgreSQL + pgvector | 唯一检索路径。 |
| 任务、工具、审批、反馈事件与检查点 | PostgreSQL | 追加式事件；检查点为当前任务投影。 |
| 情景、画像和程序记忆 | PostgreSQL + pgvector | 由事件派生，受审批、ACL、保留期控制。 |
| 当前任务临时观察、响应缓存、锁、队列 | Redis | 全部带 tenant/user scope 与 TTL；可丢失。 |

## 4. 关系模型

所有租户数据表均包含 `tenant_id TEXT NOT NULL`，与现有 JWT/会话中的租户标识保持一致。用户范围数据另含
`owner_id TEXT NULL`；`NULL` 只表示该租户共享，不能表示跨租户共享。

### 4.1 文档与检索

| 表 | 关键字段 | 约束与用途 |
| --- | --- | --- |
| `documents` | `document_id`、`tenant_id`、`source_uri`、`content_hash`、`version`、`status` | `UNIQUE (tenant_id, source_uri, version)`；记录 MinIO 对象和 ACL。 |
| `document_chunks` | `chunk_id`、`document_id`、`text`、`lexemes`、`fts`、`embedding vector(n)` | `fts` 是由现有 `jieba` 分词结果生成的 `tsvector`；`embedding` 由当前 embedding 模型生成。 |
| `document_acl` | `document_id`、`subject_type`、`subject_id`、`permission` | 只允许 `tenant`、`role`、`user` 三类主体；检索查询必须 join ACL。 |

索引：`GIN (fts)` 用于全文召回，`HNSW (embedding vector_cosine_ops)` 用于向量召回，
`BTREE (tenant_id, status)` 用于先行过滤。向量维度由已配置 embedding 模型确定，首次
迁移时写死为已验收维度；更换 embedding 模型必须建立新列/新索引和新版本，不混用向量。

### 4.2 事件、任务与记忆

| 表 | 关键字段 | 约束与用途 |
| --- | --- | --- |
| `agent_events` | `event_id UUID`、`task_id`、`event_type`、`payload_json`、`occurred_at` | 只允许 `INSERT`；任务、工具、审批、反馈和删除均以事件记录。 |
| `task_checkpoints` | `task_id`、`state`、`plan_json`、`current_step`、`version` | 当前任务状态投影；以乐观锁更新。 |
| `memories` | `memory_id`、`kind`、`content`、`embedding`、`status`、`source_event_id`、`valid_until` | `kind` 为 episodic/profile/procedural；状态为 candidate/approved/superseded/deleted。 |
| `memory_versions` | `memory_id`、`supersedes_memory_id`、`decision_event_id` | 保留冲突与更正链，不原地覆盖事实。 |
| `deletion_requests` | `request_id`、`target_type`、`target_id`、`requested_by`、`completed_at` | 记录删除请求、执行状态和失败原因。 |

`working` 记忆不进入 `memories`，仅存在 Agent Runtime 状态和 Redis TTL；`semantic`
记忆是 `documents`/`document_chunks`，不复制为另一套长期记忆记录。

## 5. RLS 与连接规则

应用数据库角色不拥有表，也不具备 `BYPASSRLS`。每一个业务事务开始后都必须执行：

```sql
SET LOCAL app.tenant_id = :tenant_id;
SET LOCAL app.user_id = :user_id;
SET LOCAL app.role = :role;
```

RLS 策略以 `current_setting('app.tenant_id', true)` 过滤租户，以 owner、角色与
`document_acl` 过滤用户访问。管理员只在其自身租户内拥有管理权限。后台 Job 使用独立
受限角色，必须显式设置目标租户；不得使用超级用户连接处理业务请求。

任何按 ID 查询、向量查询、全文查询和删除都必须在同一事务内设置 scope。连接释放前由
事务回滚清除 `SET LOCAL`，防止连接池把上一个租户带入下一个请求。

## 6. 写入流程

### 6.1 文档入库

1. 上传原件到 MinIO，得到不可变 `source_uri` 与内容哈希。
2. 在 PostgreSQL 创建 `documents`、ACL 和 `document_ingested` 事件。
3. 用已有 chunker 切块；用已有 SentenceTransformer 生成 embedding。
4. 写入 `document_chunks.text`、`lexemes`、`fts` 与 `embedding`，整批事务提交。
5. 将文档状态置为 `ready`；失败则为 `failed`，不会暴露部分 chunk。

同一 `(tenant_id, source_uri, content_hash)` 重复提交返回已有版本，不重复嵌入。文档更新
创建新版本；旧版本仅在审批/保留策略允许时归档或删除。

### 6.2 长期记忆写入

1. Agent Runtime、工具、反馈或用户显式输入先写入 `agent_events`。
2. 仅允许白名单事件生成候选记忆；训练、发布和未审核反馈不得自动生成记忆。
3. 写入前执行 PII 分类/脱敏、ACL 绑定、内容哈希去重和冲突检测。
4. 新记录状态为 `candidate`；画像和程序记忆须经用户或管理员批准才转为 `approved`。
5. 更正产生新记录与 `memory_versions` 关系；旧记录转为 `superseded`。

候选、过期、删除和被替代记忆均不可进入检索结果。

## 7. 检索与上下文构建

`MemoryOrchestrator.retrieve(identity, task, query)` 在一个受 RLS 保护的事务中执行：

1. 从 Redis 读取当前任务的 TTL 工作记忆。
2. 从 `memories` 读取已批准、未过期、且 ACL 允许的情景/画像/程序记忆。
3. 对 `document_chunks` 同时执行：全文 Top-K 和向量 Top-K。
4. 用 Reciprocal Rank Fusion 合并两个候选列表，按 `chunk_id` 去重。
5. 将文档候选和长期记忆候选一起交给现有 CrossEncoder 重排序。
6. 按类别预算截断，生成带 `source_uri`、`document_version`、`memory_id`、
   `source_event_id` 与分数的上下文。

查询顺序必须先经过 RLS、状态和 ACL 过滤，再计算召回；禁止先召回后在 Python 层过滤。
CrossEncoder 只调整排序，不授予访问权限。

初始预算：文档候选 20、全文候选 20、记忆候选 10、精排后上下文 8。它们是配置常量，
由 Phase 2 评测校准，不为每个用户开放配置。

## 8. 删除、过期与恢复

删除不是物理静默删除：先追加 `memory_deleted` 或 `document_deleted` 事件，再在事务中将
记录标记为 `deleted`，使其立即退出 RLS 检索；异步任务随后删除向量、缓存和可删除的
MinIO 对象。完成后写入 `deletion_requests.completed_at`。

文档删除只影响该文档及其 chunk，不影响源于其他事件的记忆。用户画像删除只影响该用户
范围记录。恢复使用 PostgreSQL 备份加 MinIO 对象版本/manifest；不恢复 FAISS、SQLite 或
BM25 文件。

## 9. 模块变更

| 现有模块 | Phase 2 处理 |
| --- | --- |
| `src/rag/vector_store.py` | 改为 PostgreSQL 文档仓储；删除 FAISS 文件、SQLite 元数据和 S3 索引同步。 |
| `src/rag/retriever.py` | 改为 pgvector + PostgreSQL FTS + RRF；保留 CrossEncoder 延迟加载与 CPU 默认值。 |
| `src/core/agent_runtime.py` | 将 SQLite 事件/检查点替换为 PostgreSQL 表；保持既有 API、权限和幂等工具语义。 |
| `src/inference/cache.py` | 删除语义索引持久化；保留命名空间化缓存、会话、锁与 TTL。 |
| Helm | 增加 PostgreSQL 服务、PVC、Secret、备份 Job、`DATABASE_URL` 与健康检查。 |
| `pyproject.toml` | 加入最小 PostgreSQL 驱动；完成替换后移除 `faiss-cpu` 与 `rank-bm25`。 |

不引入 SQLAlchemy：本项目的迁移和查询范围可由版本化 SQL 与一个小型 PostgreSQL 访问层
覆盖。不得保留旧索引兼容代码或“临时”双写开关。

## 10. 实施切片与验收

| 切片 | 交付 | 完成条件 |
| --- | --- | --- |
| P2-1 基础设施 | Helm PostgreSQL/pgvector、Secret、备份、版本化 SQL、RLS 验证 | 迁移、备份恢复、跨租户 SQL 拒绝均通过。 |
| P2-2 文档检索 | documents/chunks/ACL、入库、FTS、pgvector、RRF、CrossEncoder | 20～50 条评测集的召回、引用和延迟达到预先记录的门槛。 |
| P2-3 Runtime 事件 | PostgreSQL 事件与检查点、工具/审批事件接入 | Phase 1 的暂停、恢复、重试、审批和幂等测试通过。 |
| P2-4 可信记忆 | 候选、审批、冲突、过期、删除与 Orchestrator | 未批准/过期/跨租户/已删除内容均为零召回。 |
| P2-5 发布验证 | 全量测试、真实任务、备份恢复、删除传播、Helm 验证 | 所有退出门禁通过，且不存在 FAISS/BM25/SQLite RAG 运行依赖。 |

## 11. 最终退出门禁

- FAISS、`rank_bm25`、RAG SQLite 元数据及其 S3 索引同步代码和依赖均已移除；
- PostgreSQL 是文档、向量、全文、事件、检查点和长期记忆的唯一权威查询路径；
- RLS 集成测试证明跨租户文档、记忆、任务和删除请求均无法读取或修改；
- 每个 Prompt 记忆片段均可追溯到文档版本或事件、ACL 与审批决定；
- 候选、未授权、过期、被替代及已删除记忆的召回数为零；
- 删除同时覆盖 PostgreSQL 记录/向量、Redis 派生缓存和可删除 MinIO 产物；
- 记忆评测与五个 Phase 1 真实任务均不低于预先记录的完成率和质量门槛；
- PostgreSQL + MinIO 备份恢复与 Helm 部署验证通过。

## 12. 明确不做

- 不迁移或读取历史 FAISS、SQLite、BM25 索引；
- 不保留双写、影子流量或旧检索回退路径；
- 不让模型自动把任意对话、反馈或工具输出写入长期记忆；
- 不将 Redis 变成长期记忆数据库；
- 不以图数据库、多智能体或第三方记忆框架替代本阶段的最小数据模型。
