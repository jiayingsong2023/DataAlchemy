# H5 实施状态

状态：工程实现与真实 k3d/GPU 预演已贯通；canonical 发布镜像重建仍未关闭。真实代表性数据、
独立人工校准和隔离 candidate runtime 的生产资格认证已显式转入
[H6 设计](./H6_PILOT_GA_DESIGN.md)，原安全门禁不降低。

已完成的工程项：

- `013_harness_evaluation_learning.sql`：evaluation/trial、统一 annotation、不可变
  train/validation snapshot、adapter manifest、release 关联、RLS 和撤销传播字段。
- `EvaluationService`、H5 verifier、受控 `lora_train/model_evaluate` Job、固定 suite
  evaluator、adapter hash 校验和 `single_tenant_lora` 加载边界。
- model-evaluate worker 会回写 trial/campaign 状态，并校验 candidate 与 base evaluation
  的 suite/policy/trial 数一致。
- `ReleaseGovernance` 的 H5 maker-checker、CAS 版本、样本/窗口/安全门禁和 rollback；
  WebUI 提供 annotation、snapshot、adapter、release 状态与审核入口。
- Helm 为训练/评测 Job 增加独立 `HARNESS_JOB_IMAGE` 配置，避免把 Spark-only 镜像误当成
  H5 模型 Job 镜像。
- Kubernetes backend 为模型 Job 增加显式、默认关闭的 `HARNESS_JOB_GPU_ENABLED`；在本地
  k3d 没有 AMD device plugin 时，它按配置挂载 `/dev/kfd` 与 `/dev/dri`，Spark Job 不会获得
  GPU 设备。
- 嵌套 k3s/containerd 还需要单独显式设置 `HARNESS_JOB_GPU_PRIVILEGED=true`；默认仍为
  非特权。该本地-only 组合使用 `privileged` + `seccomp=Unconfined` 通过真实 GPU Job，
  不能作为多租户生产默认配置。
- 在新建、带 AMD CDI GPU request 的 `dataalchemy-gpu` k3d 集群中，普通 Pod 仍因嵌套
  containerd device cgroup 返回 `torch.cuda=False`；开启上述显式 privileged 预检后真实
  Kubernetes Job 已通过：HIP `7.1.25424`、`AMD Radeon 8060S`、`torch.cuda=True`。
- 在未安装 AMD Toolkit 时，尝试创建隔离 `dataalchemy-gpu` 集群曾返回
  `could not select device driver "" with capabilities: [[gpu]]` 并自动回滚；说明当前
  Docker/k3d 没有 AMD GPU runtime 配置，不能用模拟结果替代真实 GPU Job。
- AMD Container Toolkit 1.3.0 已安装，CDI `/etc/cdi/amd.json` 已生成并验证；Docker CDI
  测试和隔离 `dataalchemy-gpu` k3d 集群已通过 GPU 访问。现有 `dataalchemy` 集群未修改。
- ROCm Python wheels 已下载并按 `uv.lock` 完成 SHA-256 校验：
  `torch` 1,541,139,407 bytes
  (`bff09fce55656db5954b7b79b007994d8421c9ef718e5681f686951af8b2a7ad`)、`triton`
  287,185,318 bytes
  (`ca50f1cbe8a92fb9976959c7d8ad4d60ec701d452cd4035b27db3153e19ef5f1`)、`torchvision`
  2,917,207 bytes
  (`de56228db2e2d1bad12d59e0f3f2caaee459413f6a7d47ca839ab4b3b1aa0e28`) 和 `torchaudio`
  488,613 bytes
  (`a5199a0ed3329b0a3ca24b21fd9dfb1e4366f89022a30cd4c78b6d4c797db948`)。它们保存在本机
  Downloads 缓存，不进入 Git。
- `Dockerfile` 已补齐 PyTorch ROCm 运行时显式依赖（hipblas/hipfft/hiprand/hipsparse/
  hipsparselt/hipsolver/rccl/rocfft/rocsolver/rocsparse）；干净构建不应再依赖从宿主复制
  `/opt/rocm` 动态库。
- 已用上述 wheel、ROCm `.deb` 缓存和必要 runtime 库构建本地离线验证标签
  `data-alchemy:h5-canonical-offline`（image digest
  `sha256:fa796613d6d535e9a063bfb1ad1140160510721832fafa010fc06cde5179184c`）。宿主
  Docker 运行验证通过：PyTorch/HIP、`transformers`、`peft`、`datasets` 均可加载，且
  `torch.cuda=True`、设备为 `AMD Radeon 8060S`。
- 该验证标签已用 `k3d image import --mode direct` 导入隔离 `dataalchemy-gpu`，真实
  privileged Kubernetes preflight Job 通过，日志确认 `torch.cuda=True`、HIP
  `7.1.25424`、`AMD Radeon 8060S` 以及 H5 Python 依赖版本。该 Job 仅用于本地 GPU
  验收，`HARNESS_JOB_GPU_PRIVILEGED` 默认仍为关闭。
- 本轮从当前源树组装的 cache-backed 候选镜像 digest 为
  `sha256:88550d3c3a861a0c199db3721218424a7c63f06b7b6f985adfaaec94f4550079`，并在隔离
  `dataalchemy-gpu` 中完成同样的 privileged preflight；镜像 label 明确标记
  `org.dataalchemy.h5.provenance=local-rocm-cache`。
- 已准备并校验离线缓存：
  `/home/jack/Downloads/dataalchemy-h5-debs` 包含 `hipblaslt`、`miopen-hip`、`rocblas`、
  `rocrand`，均已按 ROCm 7.1.1 apt 索引 SHA-256 校验；
  `/home/jack/Downloads/dataalchemy-h5-toolkit` 包含 `amd-container-toolkit_1.3.0~24.04`。
  Toolkit 安装、写入 `/etc/cdi/amd.json` 和 Docker 重启均已完成。
- `Dockerfile` 增加 `harness-job` target；它基于 AMD 官方 ROCm runtime，
  `Dockerfile.harness` 继续只构建 Spark rough-clean 镜像。

本轮已完成一次真实基础设施工程预演（不会替代最终 H5/GA 门禁）：

- 在隔离 `dataalchemy-gpu` k3d 集群中，使用宿主 ROCm 7.2 用户态挂载、AMD GPU 设备和
  本地 H5 Job 镜像，完成 H2 evidence、base evaluation、approved snapshot、真实 LoRA、
  adapter evaluation、rollback injection 和 promote。
- 预演报告：`H5_RELEASE_REHEARSAL`，`classification=SIMULATION`；最近一次报告的
  `tenant_id=h5-simulation-ff7daf15-0581-4a5f-b0e3-e3325bd5919d`，
  `snapshot_id=42316254-7b1a-40c2-b32c-32792ce25809`，
  `adapter_id=9f4d21c6-7592-4f56-ad24-100873f437c6`，
  `rollback_release_id=98fd5b27-6a80-41c4-8405-de911bf3bc0f`，
  `promoted_release_id=8bbda151-2050-4bbc-a2e0-d92124931647`。
- 预演使用本地短训练 overlay（3 steps、128 tokens、batch 1），只证明 Job 编排、GPU
  执行、artifact allowlist、评测与发布治理闭环；它不是质量或生产性能结论。
- 预演期间发现并修复两项真实问题：训练必须使用 approved context 绑定的 `model_id`，
  以及 PEFT 生成的 `README.md` 必须在上传前剔除以保持 safetensors/JSON artifact allowlist。

本地证据：

```text
10 passed, 1 skipped (H5/runtime and GPU-mount contract tests)
DATABASE_URL=... scripts/migrate_postgres.py -> applied 013_harness_evaluation_learning.sql
PostgreSQL evaluation/snapshot/release smoke -> passed
Helm template -> rendered
```

全量回归目前为 `45 passed, 37 skipped`；生产配置测试已补齐独立的
`VERIFIER_DATABASE_URL`，没有放宽只读验证器要求。

尚未关闭的门禁及归属：

1. `docker build --target harness-job -t data-alchemy:h5-canonical .` 的干净构建仍未关闭：
   当前网络无法稳定完成约 2 GB 的 ROCm runtime apt 下载；本轮已完成 wheel/`.deb` 缓存和
   cache-backed 运行验证，但 `data-alchemy:h5-canonical` 当前产物仍带有 local cache provenance，
   不能替代可由 Dockerfile + registry 重建的发布镜像。该 clean-build 门禁仍需在可用的
   ROCm apt mirror/registry builder 上重跑。
2. **H6 资格门禁**：尚未用真实代表性业务数据和独立人工抽样/校准完成质量验收；本轮数据与
   evaluator assertion 均为 synthetic/simulation。
3. **H6 资格门禁**：尚未提供与 stable 隔离的真实 candidate runtime，因此生产 shadow/canary、
   最小样本窗口和自动 rollback 仍不能宣称通过；本轮 rollback/promote 仅为治理服务预演。

第 1 项完成后才可关闭 H5 工程交付；第 2--3 项由 H6 `PILOT_READY` 承接，并继续要求保存 H2
manifest、adapter/evaluation/release 审计证据。阶段归属变化不代表门禁已通过；单元测试或模拟 Job
不能替代真实资格认证。
