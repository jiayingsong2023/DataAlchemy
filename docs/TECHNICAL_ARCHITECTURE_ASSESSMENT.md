# DataAlchemy 技术架构评估与改造建议

> 当前状态更新（2026-07-30）：本报告主体保留评估时的历史发现。Phase 2 已将当前检索
> 收敛到 PostgreSQL pgvector + PostgreSQL FTS + RRF，FAISS、BM25 文件索引和 SQLite
> RAG 元数据不再是权威线上路径；Phase 3--4 已补充运行 manifest、治理审计、受控发布
> 与隔离恢复。仍未由代码替代的项目是 `GA-01` 真实双团队四周试点。当前事实见
> [Phase 0--4 交付总览](./PHASE_DELIVERY_SUMMARY.md)。

> 评估日期：2026-07-29  
> 评估分支：`AINativeEnhancement`  
> 评估范围：应用代码、数据流水线、推理链路、部署配置、测试与项目文档

## 1. 总体结论

DataAlchemy 已经形成“企业数据采集 → 清洗 → RAG/SFT → LoRA → 推理 → 反馈”的完整技术原型，技术覆盖面和方向感较强。其中，混合检索、本地模型路径、AMD ROCm 优化、MinIO 资产同步以及 Kubernetes 部署均有实际代码实现，并非只有架构设计。

但项目当前更接近功能丰富的 PoC，而不是可安全运行、稳定演进的企业级平台。主要差距不在功能数量，而在以下基础能力：

- 多用户数据隔离和最小权限；
- 流水线失败传播和结果可信度；
- 模型、索引与缓存的版本一致性；
- 自动训练前后的质量评测与发布门禁；
- 包管理、静态检查和端到端测试基线。

建议停止继续扩展 Agent、接口和编排层，优先将系统收敛为“模块化单体 + Kubernetes 批处理任务 + 版本化对象存储”。

## 2. 当前架构概览

当前系统由四条主要链路组成：

1. **数据链路**：Jira、Git PR、Confluence、文档和反馈数据进入 Spark 清洗、去重和分块流程。
2. **知识链路**：清洗结果经过 FAISS、BM25 和 CrossEncoder 构建混合检索索引，并同步至 MinIO。
3. **训练链路**：外部 LLM 将企业语料合成为 SFT 数据，随后使用 LoRA 微调本地基础模型。
4. **推理链路**：RAG 事实、LoRA 模型输出和外部决策模型进行融合，最终通过 FastAPI WebUI 返回结果并收集反馈。

控制与部署层同时包含：

- `Coordinator`、`AgentManager` 和 `PipelineManager`；
- APScheduler 周期任务；
- Kubernetes Job；
- Kopf Operator 和自定义资源；
- Helm Chart；
- MinIO、Redis 和本地持久卷。

## 3. 技术亮点

### 3.1 数据闭环设计完整

`PipelineManager` 已串联清洗、SFT 合成、索引和训练阶段；`Coordinator` 则统一了 CLI、WebUI 和在线问答入口。项目已经具备从原始数据到模型反馈的完整主链路。

这套设计的价值在于：

- 数据处理、知识库和模型训练共享同一份资产体系；
- 用户反馈能够重新进入数据管道；
- 每个阶段可以单独触发，也可以执行完整周期；
- 未来可以在统一链路中加入评测和审批门禁。

### 3.2 RAG 技术选型务实

当前检索路径组合了：

- FAISS 语义召回；
- BM25 关键词召回；
- CrossEncoder 深度重排；
- SQLite 正文和元数据存储；
- MinIO 跨实例持久化。

这一组合兼顾了中文语义匹配、精确关键词检索、本地运行成本和数据可迁移性，是当前项目中完成度最高、工程价值最明确的模块。

### 3.3 面向 AMD GPU 的资源优化方向明确

项目针对 AMD AI Max+ 395 和 ROCm 实现了：

- 模型延迟加载；
- FP16 推理和训练；
- 动态批处理；
- `torch.compile`；
- LoRA 适配器热加载；
- GPU 显存释放；
- 本地模型和离线加载路径。

这表明项目具有明确的目标硬件和部署场景，而不是通用 AI 示例代码。

### 3.4 具备初步云原生交付能力

仓库已经包含 Helm Chart、Operator、CRD、探针、Prometheus 指标、RBAC 以及 MinIO/Redis 持久化配置。本次评估中 `helm lint deploy/charts/data-alchemy` 执行通过，说明部署模板至少在静态结构上闭合。

### 3.5 模型配置开始与业务代码分离

`models.yaml` 集中管理合成模型、嵌入模型、重排模型、LoRA 基座和最终决策模型。该方向有利于替换模型、支持离线部署和控制运行成本。

## 4. 主要不足与风险

### 4.1 P0：用户和数据隔离不完整

会话读取和消息追加只使用 `session_id`，没有验证该会话是否属于当前登录用户。只要获得其他会话 ID，已认证用户就可能读取或写入其他用户的会话。

推理缓存同样没有包含用户、租户、模型版本或知识库版本：

- 精确缓存键只包含 prompt 和生成参数；
- 语义缓存为所有用户共享；
- 模型或索引更新后，旧答案仍可能继续命中；
- 企业内部答案可能跨用户返回。

这是当前最优先的安全问题。

### 4.2 P0：默认凭据和运行权限不适合企业环境

当前配置允许使用：

- 固定 JWT 默认密钥；
- `admin/admin123` 默认账户；
- 默认 MinIO 管理凭据；
- `privileged: true` 容器；
- 对 Pod、Service 和 Job 的通配 RBAC 权限。

配置缺失时系统主要输出警告，而不是拒绝启动。对于承载企业数据和 GPU 设备的服务，这一策略风险过高。

### 4.3 P0：流水线可能产生“假成功”

多个核心组件捕获异常后只记录日志或返回 `False`、空列表：

- SFT 合成失败后，流水线仍可能继续索引和训练；
- Agent A 返回错误状态时，完整周期没有统一检查；
- S3 上传失败可能被当作局部警告；
- Scheduler 在下层吞掉异常时仍可能记录周期成功。

因此，当前“全周期成功”不能证明所有阶段使用了本次运行产生的有效数据。

### 4.4 P0：隐私保护能力没有贯通主链路

仓库中实现了 Presidio 封装和 `advanced_sanitize_udf`，但：

- Presidio 相关包和语言模型没有在项目依赖中声明；
- 各数据清洗器仍主要调用基础正则 `sanitize_udf`；
- Presidio 不可用时会静默降级；
- 完整推理路径仍会把查询、RAG 上下文和 LoRA 输出发送给外部 DeepSeek 服务。

因此，“高级 PII 保护”和“完全离线”目前只能视为部分实现，不能作为企业级能力承诺。

### 4.5 P1：模型和知识资产缺乏原子版本管理

FAISS 索引、SQLite 元数据和 BM25 缓存作为三个独立文件上传和下载。后台同步期间，在线请求可能观察到不同版本的文件。

LoRA 适配器也直接下载到活动目录，然后在共享模型对象上重新加载，缺少：

- 不可变版本目录；
- 完整性校验；
- 原子切换；
- 推理与热加载之间的并发锁；
- 失败后的自动回滚。

### 4.6 P1：动态批处理存在静默参数错误

`BatchInferenceEngine` 会将队列中的请求合并为批次，但整批推理使用第一条请求的 `generation_kwargs`。当并发请求具有不同的 temperature、max tokens 或采样参数时，后续请求会得到不符合调用者要求的结果。

这类问题不会主动报错，因而比显式失败更难发现。

### 4.7 P1：自动训练缺少质量门禁

当前闭环的核心条件是“任务执行成功”，而不是“新版本效果优于旧版本”。尚未看到以下自动化环节：

- 输入数据质量报告；
- PII 泄漏检测；
- RAG Recall、MRR、nDCG 或答案忠实度评测；
- LoRA 训练前后 A/B 对比；
- 灾难性遗忘检查；
- 人工审批或自动晋级阈值；
- 新版本失败后的回滚。

因此，“自进化”目前更准确的描述是“自动重训和同步”。

### 4.8 P1：工程基线不足

本次验证结果如下：

| 检查 | 结果 |
|---|---|
| `.venv/bin/pytest -q` | 5 个测试通过，但主要是配置和 mock 冒烟测试 |
| `.venv/bin/ruff check .` | 418 个问题 |
| `python -m compileall` | `scripts/core/test_gpu.py` 存在语法错误 |
| `data-alchemy --help` | `ModuleNotFoundError: No module named 'src'` |
| `clean-data --help` | `ModuleNotFoundError: No module named 'src'` |
| `train-lora --help` | `ModuleNotFoundError: No module named 'src'` |
| `helm lint` | 通过 |

当前测试没有覆盖真实的数据清洗、检索质量、Redis 隔离、S3 失败、模型热加载、Operator 调谐和完整端到端流程。

### 4.9 P2：文档能力与主链路实现存在偏差

以下能力已有代码雏形，但尚未真正接入或达到文档描述：

- `MarkdownChunker` 和 `RecursiveChunker` 没有接入当前主清洗链路；
- Validator 使用当前 schema 的列验证当前 schema，自验证不能发现契约变化；
- 数据漂移检查发现偏差后仍固定返回 `True`；
- Quant RAG 目前只添加通用标记和固定加分，未建立文档与数值特征的真实关联；
- “共享单个嵌入模型”与当前 CacheManager、VectorStore 分别加载模型的实现不一致；
- `AgentInterface` 没有形成实际统一约束，`StorageInterface` 的返回类型也与实现不一致。

这些问题容易让项目表现为“概念完整、运行路径不完整”。

### 4.10 P2：技术栈与部署层次偏重

约 5.6K 行 Python 代码同时承担 Spark、Polars、Kubernetes Operator、Helm、MinIO、Redis、FAISS、LoRA、FastAPI 和多 Agent 编排。

Web、训练、Spark、Kubernetes 和 ROCm 依赖集中在同一个项目环境和基础镜像中，会带来：

- 安装和构建时间增加；
- 镜像体积和漏洞面扩大；
- WebUI 被迫加载与请求无关的 Kubernetes/GPU 组件；
- 各组件无法独立升级和验证；
- 本地开发门槛偏高。

## 5. 推荐目标架构

```text
FastAPI / WebUI
  ├── 鉴权与租户边界
  ├── RAG + LoRA 在线推理
  └── Redis：会话与版本化缓存

Kubernetes CronJob / Job
  └── 清洗 → 合成 → 评测 → 训练 → 发布

MinIO
  └── runs/{run_id}/
      ├── 输入快照与数据质量报告
      ├── SFT 数据
      ├── RAG 索引包
      ├── LoRA 适配器
      └── manifest.json
                         ↓
                 原子更新 current 指针
                         ↓
                 WebUI 安全热加载
```

目标架构保留当前真正有价值的能力：

- 数据闭环；
- 混合 RAG；
- ROCm 本地推理与训练；
- MinIO 资产存储；
- Redis 会话和缓存；
- Kubernetes 批处理。

默认不再同时维护 Helm Job、APScheduler 和 Operator 三套调度机制。优先使用 Kubernetes Job/CronJob；只有在需要管理多个 DataAlchemyStack 实例、持续调谐资源状态时才保留 Operator。

## 6. 分阶段改造建议

### 6.1 P0：先做到不会泄漏、不会错误发布

1. **修复身份和会话隔离**
   - Session 元数据保存 `owner`；
   - 所有读取、追加和反馈操作校验当前用户；
   - WebSocket 与 HTTP 共用同一套授权逻辑。

2. **隔离缓存**
   - 缓存键加入 tenant、user、model version、adapter version 和 index version；
   - 禁止使用 `flushdb()` 清理业务缓存；
   - 只删除 DataAlchemy 自己的键前缀。

3. **收紧安全配置**
   - 生产环境缺少 JWT 密钥时拒绝启动；
   - 默认管理员仅允许显式开发模式创建；
   - Helm 不再提供可直接使用的默认密码；
   - 移除 `privileged`；
   - 按实际 API 调用收缩 RBAC verbs 和 resources。

4. **让失败向上传播**
   - 删除关键阶段的 catch-and-continue；
   - 每个阶段输出明确结果和产物路径；
   - 任一必需产物缺失时终止本次 Job；
   - 只有发布完成后才能记录完整周期成功。

5. **明确数据外发策略**
   - 增加禁止外部 LLM 的部署模式；
   - 外发前执行真正启用的 PII 清洗；
   - 记录数据发送目的、模型和运行 ID；
   - 文档区分本地模型离线能力与完整系统离线能力。

### 6.2 P1：修复一致性和工程基线

1. **版本化发布资产**
   - 使用 `runs/{run_id}` 写入不可变产物；
   - manifest 记录文件哈希、数据版本、模型版本和评测结果；
   - 全部文件验证通过后原子更新 `current` 指针；
   - 在线服务只加载完整且已晋级的版本。

2. **修复动态批处理**
   - 以生成参数为键对请求分组；
   - 或在请求量较小时暂时取消复杂批处理，先保证语义正确。

3. **恢复可用的包入口**
   - 按当前源码布局，将入口改为 `run_agents:main`、`etl.main:main` 和 `train:train`；
   - 测试使用与生产一致的导入路径；
   - 避免继续增加 `sys.path` 修改。

4. **建立最小 CI 门禁**
   - Python 编译检查；
   - Ruff；
   - 单元和集成测试；
   - Helm lint/template；
   - 关键容器启动测试。

5. **补充最有价值的测试**
   - 用户 A 无法读取用户 B 的会话；
   - 不同生成参数不会进入同一错误批次；
   - S3、合成或训练失败时完整周期失败；
   - 索引只会切换到完整版本；
   - 新模型未通过评测时继续使用旧版本。

### 6.3 P2：收敛概念与运行成本

1. 删除未接入的 Chunker、Protocol 和 Quant 占位实现，或在完成真实业务验证后再接入。
2. 将纯数据变换恢复为普通函数或流水线步骤；只有具备独立决策、工具调用和状态的组件才称为 Agent。
3. 将依赖拆分为 API/推理、ETL、训练和开发四组。
4. 分别构建最小运行镜像，WebUI 镜像不再包含 Spark、Java、kubectl 和训练依赖。
5. 以真实数据规模决定是否启用 Spark；小规模环境优先使用 Polars 或普通 Python 批处理。

## 7. 质量与发布门禁建议

每次自动周期应产生以下最小报告：

| 阶段 | 最小门禁 |
|---|---|
| 清洗 | 输入/输出数量、错误率、去重率、空文本率、PII 命中与残留 |
| SFT 合成 | 有效率、解析失败率、重复率、毒性和敏感信息检查 |
| RAG | Recall@K、MRR/nDCG、重排收益、无答案问题表现 |
| LoRA | 固定评测集对比、灾难性遗忘检查、延迟和显存变化 |
| 发布 | 产物哈希完整、指标不低于基线、可回滚 |

新版本只有在全部必需门禁通过后才能更新线上 `current` 指针。训练完成不等于发布成功。

## 8. 最终建议

建议保留并继续投入以下四个核心方向：

1. 企业数据闭环；
2. FAISS + BM25 + CrossEncoder 混合检索；
3. AMD ROCm 本地训练和推理；
4. MinIO 中的版本化数据、索引和模型资产。

建议暂缓继续增加 Agent、Operator 能力和新的基础设施组件。当前最高收益的工作是安全隔离、失败传播、原子发布、评测门禁和工程质量基线。

完成这些改造后，DataAlchemy 才能从“功能完整的技术展示”升级为“结果可信、数据安全、可持续演进的企业 AI 数据与模型平台”。
