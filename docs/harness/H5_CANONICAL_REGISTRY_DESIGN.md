# H5 Canonical Registry-clean 镜像设计方案

> 状态：设计方案。本文只定义构建、发布和验收流程，不执行 registry 登录、镜像推送或
> Kubernetes 发布。目标是关闭 H5 的 canonical registry-clean 镜像门禁。

## 1. 目标与边界

### 1.1 目标

构建一个可以在没有本机 `.venv`、ROCm `.deb` 缓存、宿主 `/opt/rocm` 和 Maven 下载的环境中
重建并运行的 H5 镜像：

```text
Git commit
  → 干净 Docker builder
  → GHCR immutable image digest
  → 干净 dataalchemy-gpu 集群按 digest 拉取
  → GPU preflight
  → H5 evidence → LoRA → evaluation → rollback → promotion
```

### 1.2 不在本方案范围内

- 不把企业数据、PDF、反馈、数据库或 MinIO 运行产物写入镜像；
- 不把宿主 `.venv`、宿主 `/opt/rocm` 或临时下载目录复制到镜像；
- 不用 `data-alchemy:h5-canonical-local` 直接改标签后宣称 canonical；
- 不用 synthetic rehearsal 替代 H6 真实数据资格和 GA-01 外部试点。

## 2. GitHub 仓库与 GHCR 包设计

### 2.1 GHCR 不是单独的 GitHub 源码仓库

源码继续放在现有 GitHub repository。镜像首次 push 到 GitHub Container Registry 后，会自动
创建一个 Container package：

```text
源码仓库：github.com/<OWNER>/DataAlchemy
镜像包：ghcr.io/<OWNER>/data-alchemy
```

通过 OCI label 将包关联到源码仓库：

```text
org.opencontainers.image.source=https://github.com/<OWNER>/DataAlchemy
```

不需要再创建一个“只存 Dockerfile 的镜像源码仓库”。

### 2.2 可见性决策

| 选择 | 适用场景 | 风险/代价 |
| --- | --- | --- |
| Private package | 内部项目、未公开源码或不希望匿名拉取 | 需要 GHCR 权限和账号套餐配额；24GB 镜像会产生较大存储/流量压力 |
| Public package | 源码、依赖和镜像可公开 | 公开包可匿名拉取，但镜像内容和构建依赖对外可见 |

本项目默认选择 **Private**。如果源码本身公开且需要降低拉取权限复杂度，可以选择 Public；
不论可见性，都不得把 token、数据库密码、企业数据或私有模型权重写入镜像。

GitHub Packages 对公开包提供免费使用；私有包受账号套餐的存储和数据传输配额/预算约束，
当前 24GB 镜像必须先确认账号配额或企业预算，不应把它当作无限免费存储。

参考：[GitHub Packages 简介](https://docs.github.com/en/packages/learn-github-packages/introduction-to-github-packages)、
[GitHub Packages 计费](https://docs.github.com/en/billing/concepts/product-billing/github-packages)。

## 3. 镜像命名、标签和 digest

镜像名称：

```text
ghcr.io/<OWNER>/data-alchemy
```

标签规则：

| 标签 | 用途 | 是否可作为部署目标 |
| --- | --- | --- |
| `h5-canonical-<git-sha>` | 一次源码提交对应的不可变人类可读标签 | 仅用于定位；部署仍记录 digest |
| `h5-canonical` | 最近一次通过 H5 canonical 门禁的别名 | 仅用于人工查找，不作为生产唯一引用 |
| `sha256:<digest>` | registry 返回的不可变内容地址 | **唯一正式部署引用** |
| `latest` | 不使用 | 禁止用于 H5 验收和 Helm 发布 |

发布 manifest 至少保存：

- Git commit SHA、分支和工作树洁净状态；
- 镜像 digest、架构、构建时间；
- ROCm 基础镜像名称和 digest；
- `uv.lock` SHA-256；
- Dockerfile SHA-256；
- 构建器版本、BuildKit 版本和构建日志地址；
- H5 GPU preflight、LoRA、evaluation、rollback、promotion evidence URI/hash。

## 4. Canonical 构建契约

### 4.1 构建输入

只允许以下 Git tracked 输入进入构建上下文：

```text
Dockerfile
pyproject.toml
uv.lock
README.md
models.yaml
src/
webui/
```

当前 `.dockerignore` 已排除 `.venv/`、`data/`、`docs/`、`scripts/`、`tests/`、`.git/` 和
部署临时文件。构建上下文不得包含：

```text
/home/jack/Downloads/dataalchemy-h5-debs
/home/jack/Downloads/dataalchemy-h5-toolkit
/home/jack/.cache/uv
/home/jack/.venv
/opt/rocm
```

### 4.2 依赖来源

- 基础镜像：`rocm/dev-ubuntu-24.04:7.1.1`，构建日志记录实际 digest；
- Python：`uv sync --frozen`，只使用 `uv.lock`；
- PyTorch/ROCm wheels：使用 `pyproject.toml` 和 lock 中的固定 URL/hash；
- Spark 运行需要的 AWS/Hadoop JAR 已在 Spark 专用镜像中预置，H5 模型 Job 不运行时 Maven
  下载；
- 所有外部下载必须在构建日志中可追溯。若公网不稳定，使用内部 PyPI/ROCm/OCI mirror，
  但 mirror 中的 artifact 必须保留版本和 SHA-256。

### 4.3 当前 Dockerfile 需要保持的约束

构建使用 `harness-job` target：

```bash
docker build --pull --no-cache \
  --target harness-job \
  -t ghcr.io/<OWNER>/data-alchemy:h5-canonical-<GIT_SHA> .
```

构建器不得通过 bind mount 或临时 Dockerfile 提供 `.venv`、ROCm `.deb` 或 `/opt/rocm`。
本机离线包可以用于构建一个**受控基础镜像/内部 artifact**，但必须先进入版本化、可校验的
artifact registry；不能只存在于某台开发机。

为了提高可复现性，正式实施时应进一步固定：

1. ROCm base image 的 digest；
2. `kubectl` 版本，不使用动态的 `stable.txt`；
3. apt 软件源快照或内部镜像；
4. 构建器、BuildKit 和目标平台（当前为 `linux/amd64`）。

## 5. GitHub 认证与包权限

### 5.1 手工推送

命令行推送 GHCR 使用 GitHub Personal Access Token。Token 不写入仓库、`.env`、shell 历史或
Dockerfile：

```bash
export GHCR_OWNER=<OWNER>
export GHCR_IMAGE="ghcr.io/${GHCR_OWNER}/data-alchemy"
export GHCR_TOKEN='<通过安全密码管理器注入的 token>'

printf '%s' "$GHCR_TOKEN" | docker login ghcr.io \
  --username "$GHCR_OWNER" \
  --password-stdin
unset GHCR_TOKEN
```

手工 push 所需权限：

- `write:packages`：推送镜像；
- `read:packages`：拉取私有镜像；
- `delete:packages`：不作为日常权限，只有清理包时临时使用。

### 5.2 GitHub Actions

CI 优先使用仓库级 `GITHUB_TOKEN`，workflow 权限最小化：

```yaml
permissions:
  contents: read
  packages: write
```

只有需要签名或 provenance attestation 时才额外启用对应的 OIDC 权限。Pull request 默认只做
构建和验证，不推送 canonical 标签；只有受保护分支或手工批准 workflow 才能覆盖
`h5-canonical`。

## 6. 构建器与 CI 设计

### 6.1 构建器要求

由于当前镜像约 24GB，不建议直接依赖 GitHub-hosted runner 的默认磁盘。推荐使用：

- 自托管 Linux x86_64 builder，至少 100GB 可用磁盘；
- Docker BuildKit/buildx；
- 能访问 GHCR、ROCm apt mirror 和 PyPI/内部 PyPI；
- 构建阶段不需要 AMD GPU；GPU 只在后续 k3d Job 验收使用；
- registry cache 使用独立 cache ref，例如：
  `ghcr.io/<OWNER>/data-alchemy:buildcache-h5`。

### 6.2 Workflow 逻辑

建议新增 `.github/workflows/h5-canonical.yml`，逻辑如下：

```text
checkout 指定 commit
  → 检查 git 工作树/构建上下文
  → buildx login GHCR
  → build --target harness-job
  → 导出 image digest / build metadata
  → 容器静态 smoke test
  → push git-sha tag
  → 需要人工批准后再移动 h5-canonical tag
  → 生成 release manifest
```

Workflow 不应在每个普通 PR 上推送 24GB 镜像。推荐触发条件：

- `workflow_dispatch`：手工指定 commit；
- 受保护分支合并；
- 版本 tag；
- H5 发布审批通过。

## 7. 验证分层

### 7.1 构建后静态验证

```bash
docker image inspect "$IMAGE" \
  --format '{{json .RepoDigests}} {{json .Config.Labels}}'

docker run --rm "$IMAGE" python -c \
  'import torch, transformers, peft, datasets; assert torch.version.hip'

docker run --rm "$IMAGE" python -c \
  'import harness.job_runner, src.harness.evaluation, src.memory.orchestrator'
```

检查内容：

- PyTorch 存在 ROCm/HIP；
- `transformers`、`peft`、`datasets` 和 H5 Python 模块可导入；
- 镜像内不存在 `.env` 中的真实 secret；
- 镜像内没有企业 PDF、MinIO 数据、训练 adapter 或宿主路径；
- 默认命令是 `harness.job_runner`，Spark-only 镜像不会被误用于 LoRA。

### 7.2 Registry pull 验证

在本机删除或隔离原有本地 tag 后，按 digest 拉取：

```bash
docker pull "$GHCR_IMAGE@sha256:<DIGEST>"
docker image inspect "$GHCR_IMAGE@sha256:<DIGEST>"
```

这一步必须使用 registry 返回的 digest，而不是本地 tag 的 image ID。

### 7.3 干净 k3d/GPU 验证

在新建的 `dataalchemy-gpu` 集群中：

1. 使用 GHCR pull secret（私有包）或匿名拉取（公开包）；
2. Helm 的 `images.harnessJob` 指向本次 H5 canonical digest；`images.core` 与
   `images.etl` 分别指向已验证的 Web 和 ETL 镜像 digest，三者不得互相代用；
3. `imagePullPolicy` 使用 `IfNotPresent`，禁止使用本地旧 tag；
4. 执行 GPU preflight，确认 HIP、`torch.cuda=True` 和 AMD 设备名；
5. 按 H5 顺序执行：

   ```text
   H2 evidence
   → base evaluation
   → approved snapshot
   → GPU LoRA
   → adapter evaluation
   → rollback injection
   → promotion
   ```

6. 将所有 Job 名称、镜像 digest、日志和 manifest hash 写入 H5 evidence。

私有 GHCR 的 pull secret 示例：

```bash
kubectl -n data-alchemy create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io \
  --docker-username="$GHCR_OWNER" \
  --docker-password="$GHCR_READ_TOKEN"
```

Deployment/Job 的 `imagePullSecrets` 必须引用该 secret；token 不得写入 Helm values 或 Git。

## 8. Helm 配置契约

Canonical 发布时必须覆盖默认的本地 tag：

```yaml
images:
  core: ghcr.io/<OWNER>/data-alchemy@sha256:<DIGEST>
  harnessJob: ghcr.io/<OWNER>/data-alchemy@sha256:<DIGEST>
  operator: ghcr.io/<OWNER>/dataalchemy-operator@sha256:<OPERATOR_DIGEST>
  pullPolicy: IfNotPresent
```

本地 `data-alchemy:h5-canonical-local` 只允许在 `LOCAL`/`REHEARSAL` 环境使用；生产候选、
H5 canonical evidence 和 H6 qualification 不得混用本地 tag。

## 9. 失败处理与回滚

| 失败点 | 处理 |
| --- | --- |
| ROCm/PyPI 下载超时 | 切换内部 mirror 或远程 builder；不把宿主缓存复制进最终镜像 |
| Docker build 失败 | 保留构建日志和 commit，不移动 `h5-canonical` 标签 |
| push 成功但 digest 不匹配 | 丢弃该候选，禁止部署；重新读取 registry manifest |
| 静态 smoke 失败 | 删除候选 tag，修复 Dockerfile 后重新构建 |
| GPU preflight 失败 | 保留镜像 digest，标记 candidate failed，不 promotion |
| H5 evaluation/rollback 失败 | 不移动 canonical/promoted release，恢复上一 digest |
| GHCR 配额不足 | 转移到组织/企业 registry 或内部 OCI registry，不降低验收标准 |

canonical 标签只允许指向最近一次全部通过的 digest；失败构建不能覆盖已通过版本。

## 10. H5 退出门禁与交付物

### 必须通过

- [ ] 干净 builder 使用当前 Git commit 和 Dockerfile 完成 `harness-job` 构建；
- [ ] 未复制宿主 `.venv`、ROCm `.deb`、`/opt/rocm`、业务数据或 secrets；
- [ ] 镜像被推送到 GHCR，得到稳定 digest；
- [ ] 从 GHCR 按 digest 重新拉取成功；
- [ ] 干净 `dataalchemy-gpu` 集群 GPU preflight 通过；
- [ ] H2 evidence、base evaluation、approved snapshot、GPU LoRA、adapter evaluation、
  rollback 和 promotion 全部使用该 digest；
- [ ] H5 manifest 记录 commit、Dockerfile/lock hash、镜像 digest、Job evidence 和结果；
- [ ] 失败 candidate 不会覆盖 canonical 标签或已晋级 release。

### 最终交付物

```text
ghcr.io/<OWNER>/data-alchemy@sha256:<DIGEST>
H5_CANONICAL_BUILD_MANIFEST.json
H5_GPU_PREFLIGHT_MANIFEST.json
H5_RELEASE_REHEARSAL_MANIFEST.json
构建日志、push/pull 日志、Helm values 摘要和 rollback evidence
```

完成上述清单后，才能将 `docs/TODO.md` 中的 **H5 canonical 镜像** 标为 `[x]`。它仍不能
关闭 H6 的真实业务数据、人工校准、真实 candidate runtime 或 GA-01 外部团队门禁。

## 11. 推荐实施顺序

1. 确定 GHCR owner、包可见性和配额；
2. 创建 PAT 或配置 GitHub Actions `GITHUB_TOKEN`；
3. 为构建器准备磁盘、网络和 registry cache；
4. 固定 base digest、kubectl 版本和 apt/mirror 策略；
5. 执行 clean build 和静态 smoke；
6. push SHA tag，按 digest pull 验证；
7. 在新 GPU 集群执行完整 H5 rehearsal；
8. 人工复核 manifest 后再移动 `h5-canonical` 标签；
9. 更新 H5 状态报告和 `docs/TODO.md`。
