# DataAlchemy：受治理的企业智能体发布候选

DataAlchemy 将企业知识检索、持久记忆和受控工具调用收敛到一个可暂停、可恢复、可审计的
单智能体运行时。当前代码基线是 **内部发布候选**，不是已完成真实客户验收的正式生产版。

当前阶段状态与外部发布门禁见 [Phase 0--4 交付总览](docs/PHASE_DELIVERY_SUMMARY.md)。

## 当前能力

- 单一 `Plan → Act → Observe → Replan` 运行时：任务计划、事件、审批、暂停、恢复、
  重试、幂等键、限流和敏感字段脱敏均持久化。
- PostgreSQL + pgvector + PostgreSQL FTS + RRF 是文档、检索、任务事件和长期记忆的
  权威路径；CrossEncoder 只用于候选精排。
- PostgreSQL RLS 保护 tenant 边界；文档、记忆、任务、连接器运行、审计与发布记录均按
  tenant 作用域访问。
- GitHub 只读连接器同步文件正文、ACL、版本替换与删除；运行结果写入哈希校验的
  `runs/{run_id}/manifest.json`。
- WebUI 提供聊天、任务审批/恢复、连接器运行、记忆查询和管理员审计面板。
- 生产身份使用 OIDC 授权码 + PKCE；生产环境拒绝本地密码认证与默认凭据。
- 发布候选必须带评测和回滚目标，依次经历候选、影子、灰度、晋级或自动回滚。

## 明确边界

- 不使用多智能体、图数据库或第二个检索权威路径。
- Redis 仅用于带 tenant scope 和 TTL 的缓存、会话、锁及队列；MinIO 仅保存原始不可变
  对象和运行产物，不保存 RAG/记忆权威索引。
- 自动训练不是默认生产路径：只有已审核、来源完整且按 tenant 许可的反馈才能成为训练
  候选；未证明提升的 LoRA 不发布。
- `GA-01` 尚未完成：需要两支独立真实团队连续四周试点、周度审计和双方签署。内部
  Alpha、压缩预演和本地测试不能替代该门禁。

## 快速开始：本地或内部 Alpha

### 前提

- Python 3.12、[uv](https://docs.astral.sh/uv/)、Docker、Helm 3。
- 可用的 PostgreSQL 16 + pgvector、Redis 和兼容 S3 的 MinIO。
- 本地模型路径，或在允许外发时显式设置云增强配置。纯本地模式不应配置外部模型凭据。

```bash
uv sync

export DATABASE_URL='postgresql://dataalchemy_app:password@host:5432/dataalchemy'
export REDIS_URL='redis://host:6379'
export S3_ENDPOINT='http://minio:9000'
uv run python scripts/migrate_postgres.py
uv run python scripts/pilot_check.py
```

内部部署可使用：

```bash
export DATABASE_URL='postgresql://dataalchemy_app:password@host:5432/dataalchemy'
./scripts/pilot_up.sh
```

部署参数、最小 Git 服务账户与首项任务见 [Git 试点快速开始](docs/PILOT_QUICKSTART.md)。
生产部署不得提交或打印数据库密码、OIDC client secret、Git token 或原始企业数据。

## 身份与配置

开发环境可使用受控本地账户。生产环境必须设置：

```env
DATAALCHEMY_ENV=production
AUTH_MODE=oidc
AUTH_SECRET_KEY=<at-least-32-character-secret>
OIDC_AUTHORIZE_URL=https://issuer.example/authorize
OIDC_TOKEN_URL=https://issuer.example/token
OIDC_USERINFO_URL=https://issuer.example/userinfo
OIDC_CLIENT_ID=dataalchemy
OIDC_REDIRECT_URI=https://dataalchemy.example/api/auth/oidc/callback
OIDC_GROUP_ROLE_MAP={"dataalchemy-admins":"admin"}
DISABLE_DEFAULT_ADMIN=true
```

OIDC 的 subject、tenant 和 group claim 只在服务端验证和映射。将真实 OIDC 提供商接入
目标部署环境后，必须完成一次登录、角色映射和 tenant 隔离联调。

## 试点连接器与工具

Git 试点需要只读服务账户和显式读者 ACL：

```env
GIT_PILOT_REPOSITORY=organization/repository
GIT_PILOT_TOKEN=<read-only-token>
GIT_PILOT_READERS=alice,bob
PILOT_RUNS_DIR=/app/data/pilot-runs
```

管理员在 WebUI 创建 `sync_git` 任务并批准后执行同步。文件正文首先写入 PostgreSQL，
再撤销同一路径的旧版本；失败运行不推进游标。不要使用个人管理员令牌。

## 验证与发布候选检查

```bash
TEST_DATABASE_URL='postgresql://dataalchemy_app:password@host:5432/dataalchemy' \
  uv run pytest -q --ignore=tests/test_integration.py

DATABASE_URL='postgresql://dataalchemy_app:password@host:5432/dataalchemy' \
  uv run python scripts/evaluate_phase4_internal_alpha.py

helm lint deploy/charts/data-alchemy
helm template data-alchemy deploy/charts/data-alchemy >/dev/null
```

恢复演练必须使用预创建的**隔离目标数据库**，禁止将恢复命令指向源库：

```bash
PILOT_DATABASE_URL='<source-url>' \
PILOT_RESTORE_DATABASE_URL='<isolated-target-url>' \
  ./scripts/verify_pilot_restore.sh
```

完整工程证据见 [Phase 4 发布候选报告](docs/PHASE4_RELEASE_CANDIDATE_REPORT.md)。未来真实
试点的准入、审计与签署模板见 [GA-01 试点包](docs/GA01_PILOT_PACK.md)。

## 架构与历史文档

- [产品路线图](docs/PRODUCT_ROADMAP.md)
- [项目改进计划](docs/IMPROVEMENT_PLAN.md)
- [技术架构评估与当前状态说明](docs/TECHNICAL_ARCHITECTURE_ASSESSMENT.md)
- [依赖分层](docs/DEPENDENCY_LAYERS.md)

旧的 Spark/K3d 全量清洗、无门禁 LoRA 训练和 S3 RAG 索引说明不再是当前推荐生产路径；
它们保留在历史实现与部署文档中，不能作为发布候选的能力声明。
