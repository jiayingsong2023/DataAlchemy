# DataAlchemy：受治理的企业智能体发布候选

DataAlchemy 将企业知识检索、持久记忆和受控工具调用收敛到一个可暂停、可恢复、可审计的
单智能体运行时。当前代码基线是 **内部发布候选**，不是已完成真实客户验收的正式生产版。

当前阶段状态与外部发布门禁见 [发布状态](docs/RELEASE_STATUS.md)。

[![DataAlchemy 当前软件架构：点击查看原图](docs/images/dataalchemy-release-candidate-architecture.svg)](docs/images/dataalchemy-release-candidate-architecture.svg)

## 当前能力

- 单一 `Plan → Act → Observe → Replan` 运行时：任务计划、事件、审批、暂停、恢复、
  重试、幂等键、限流和敏感字段脱敏均持久化。
- PostgreSQL + pgvector + PostgreSQL FTS + RRF 是文档、检索、任务事件和长期记忆的
  权威路径；CrossEncoder 只用于候选精排。
- PostgreSQL RLS 保护 tenant 边界；文档、记忆、任务、连接器运行、审计与发布记录均按
  tenant 作用域访问。
- GitHub 只读连接器先将文件版本、ACL 和原始对象落入受限接入区；经过类型、大小、
  编码、密钥和路径门禁及分块后，才原子发布到 PostgreSQL 检索文档。
- PDF/DOCX 已有受控单文件试点：WebUI 上传后进入 MinIO raw、真实 Spark rough clean、
  deterministic refine、PostgreSQL 发布和 RAG 引用；Git 有独立的只读同步路径。Jira、
  Confluence、Git PR 的清洗器可复用 Spark，但外部连接器尚未作为当前试点入口承诺；邮箱
  连接器尚未实现。
- WebUI 提供聊天、任务审批/恢复、连接器运行、记忆查询和管理员审计面板。
- 问答默认以 RAG 引用为事实根据；证据不足时拒答。显式启用云模式后，
  可由 DeepSeek 融合 RAG context 与已加载 adapter 的 intuition，外发前必须通过
  Presidio 脱敏并写入 cloud audit。
- 生产身份使用 OIDC 授权码 + PKCE；生产环境拒绝本地密码认证与默认凭据。
- 发布候选必须带评测和回滚目标，依次经历候选、影子、灰度、晋级或自动回滚。

## 明确边界

- 不使用多智能体、图数据库或第二个检索权威路径。
- Redis 仅用于带 tenant scope 和 TTL 的缓存、会话、锁及队列；MinIO 仅保存原始不可变
  对象和运行产物，不保存 RAG/记忆权威索引。
- Memory distillation、训练快照、LoRA、固定评测和受控发布代码已经存在，但都受来源、
  tenant、人工审核和发布门禁约束；上传 PDF 不会自动训练或发布 adapter。
- H5 的 canonical registry 镜像门禁仍未关闭；H6 真实代表性数据、独立人工校准、candidate
  runtime 和 `GA-01` 尚未完成。两支独立真实团队连续四周试点、周度审计和签署仍是正式 GA
  门禁，内部 Alpha、模拟预演和本地测试不能替代它。

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

本地 GPU 集群的删除、重建、镜像导入、部署、PDF 入库、Memory 和 H5 验收见
[本地环境操作手册](docs/LOCAL_ENVIRONMENT_OPERATIONS.md)；产品闭环说明见
[内部试点快速开始](docs/PILOT_QUICKSTART.md)。`scripts/pilot_up.sh` 只适用于已有外部
依赖的快速检查，不负责清理或重建本地 GPU 集群。

当前从 PDF 到 WebUI/H5 的固定入口是两阶段 CLI：

```bash
.venv/bin/python scripts/run_pdf_full_cycle.py --stage webui --pdf data/input/pilot.pdf
.venv/bin/python scripts/run_pdf_full_cycle.py --stage h5 --run-id <run_id> --suite data/input/pdf-suite.json
```

第一阶段使文档可在 WebUI 检索和问答；用户对话、记忆提炼与反馈审核后，第二阶段
才会进入训练快照、GPU LoRA、评测、发布预演和 model reload。生产模式不允许自动审批。
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

管理员在 WebUI 创建 `sync_git` 任务并批准后执行同步。原始文件先进入 MinIO 受限接入区，
通过清洗门禁后才原子发布到 PostgreSQL；再撤销同一路径的旧版本。失败运行不推进游标。
不要使用个人管理员令牌。

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

完整工程证据见 [Phase 4 发布候选报告](docs/release/PHASE4_RELEASE_CANDIDATE_REPORT.md)、
[H5 实施状态](docs/harness/H5_IMPLEMENTATION_STATUS.md) 和 [H6 实施状态](docs/harness/H6_IMPLEMENTATION_STATUS.md)。
未来真实试点的准入、审计与签署模板见 [GA-01 试点包](docs/release/GA01_PILOT_PACK.md)。

## 架构与历史文档

- [当前软件架构](docs/ARCHITECTURE.md)
- [一份文档的内部试点快速开始](docs/PILOT_QUICKSTART.md)
- [本地 GPU 集群操作手册](docs/LOCAL_ENVIRONMENT_OPERATIONS.md)
- [发布状态与 GA-01 门禁](docs/RELEASE_STATUS.md)
- [当前待办清单](docs/TODO.md)
- [Agent Harness 执行计划](docs/AGENT_HARNESS_EXECUTION_PLAN.md)

Spark 仍是大规模历史回灌与批量粗清洗的执行引擎；K3d 仅用于本地集群验证。无门禁 LoRA
训练和 S3 RAG 索引保留在历史实现与部署文档中，不能作为发布候选的能力声明。
历史评估、阶段计划和旧运维记录见 [docs/archive](docs/archive/README.md)。
