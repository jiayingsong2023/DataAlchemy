# Ray Data 候选评估路线图

> 状态：候选，尚未启用  
> 复核日期：2026-09-02  
> 目标：判断 Ray Data 是否应成为 AI 密集型训练数据加工后端，而不是预先决定替换 Spark。

## 1. 当前结论

Ray Data 暂不进入运行依赖、部署清单或发布能力声明。当前单 PDF/DOCX 试点规模小，默认应优先
采用普通 Python Job；Spark 继续承担已存在的批量粗清洗和 MinHash LSH 路径。只有真实训练数据
生产出现 CPU/GPU 混合流水线，并且受控 A/B 证明 Ray Data 在目标负载上明显获益时，才进入迁移。

Ray Data 的候选范围限定为：批量 embedding、LLM 标注/评分、合成 QA、模型过滤，以及向训练
worker 流式供数。它不替代 `AgentRuntime`、Tool Gateway、PostgreSQL、MinIO、任务审批、独立
verifier 或发布治理。

官方能力依据：

- [Ray Data 执行模型](https://docs.ray.io/en/latest/data/data-internals.html)：基于 block 的流式执行；
- [Ray Data LLM](https://docs.ray.io/en/latest/data/working-with-llms.html)：批量 LLM 推理与多阶段处理；
- [Ray Train 数据预处理](https://docs.ray.io/en/latest/train/user-guides/data-loading-preprocessing.html)：
  训练前处理与训练 worker 的流式衔接；
- [Ray 加速器支持](https://docs.ray.io/en/latest/ray-core/scheduling/accelerators.html)：AMD GPU
  当前为实验性、社区支持，必须单独验证 ROCm 稳定性。

## 2. 使用边界

| 场景 | 默认执行方式 | Ray Data 定位 |
| --- | --- | --- |
| 单 PDF/DOCX 解析、规则清洗、PII 脱敏 | 普通 Python Job | 不使用 |
| 小批量文档接入和增量同步 | 普通 Python Job | 不使用 |
| 大规模结构化合并、Parquet、MinHash LSH | 当前保留 Spark | 只有 A/B 通过才迁移 |
| 批量 embedding、LLM 标注、质量评分 | 尚未形成真实生产负载 | 首要候选 |
| 合成 QA、模型过滤、训练前转换 | 现有受治理脚本/Job | 首要候选 |
| 在线问答、工具调用、任务编排 | `AgentRuntime` | 禁止接管 |

不因为数据最终用于训练就自动选择 Ray Data。引擎选择取决于数据规模、计算类型、资源拓扑、
端到端成本和可验证性。

## 3. 启动条件

以下条件全部满足后才启动 Ray Data PoC：

1. 至少一个真实训练数据任务包含 embedding、LLM 推理或模型评分等 GPU/模型阶段；
2. 单机 Python 基线已记录端到端耗时、峰值内存、吞吐、失败恢复和成本；
3. 目标数据量来自真实试点或容量规划，不为证明分布式框架而制造大规模 synthetic 数据；
4. 输入、输出、lineage、ACL、许可、decision/reason code 和 artifact hash 契约已冻结；
5. 当前 ETL 基线已恢复：`spark_rough_clean` 按 job kind 选择 ETL 镜像，且 ETL 镜像能够离线
   解析真实 PDF/DOCX。基线未通过时不得用 Ray 迁移掩盖现有回归。

没有 GPU/模型批处理阶段，或普通 Python 已满足目标吞吐时，保持 `NOT-SELECTED`。

## 4. 分阶段路线

### RD0：恢复并冻结基线

交付：

- 修复 Spark Job 与 training Job 共用镜像选择的问题；
- 补齐 ETL 镜像的 PDF/DOCX 解析依赖；
- 用真实 PDF/DOCX 在断网镜像和 Kubernetes Job 中跑通 rough clean；
- 固定一份输入 manifest、期望 rough corpus、计数、hash 和 verifier 结果。

退出门禁：同一输入可重复生成可验证产物；失败任务不会发布 `accepted` receipt。

### RD1：建立可比较工作负载

选择三档真实或脱敏数据：日常小批量、目标典型批量、容量上限。对每档记录：

- 冷启动和端到端 wall time；
- records/s、bytes/s、峰值 RSS 和临时存储；
- CPU/GPU 利用率及 GPU 空闲等待时间；
- Job/集群启动成本、重试和恢复时间；
- 输出 schema、lineage、ACL、decision/reason code 与内容 hash。

先测普通 Python，再测当前 Spark。此阶段不安装 Ray。

### RD2：最小 Ray Data PoC

只实现一条最有价值的 AI 批处理链，优先选择：

```text
normalized records → embedding 或模型评分 → filtered training candidates
```

约束：

- 先在单节点 Ray 中运行，不引入 KubeRay；
- 复用 RD1 的输入和 artifact/verifier 契约；
- 不同时重写 MinHash、训练器、发布治理或在线服务；
- 不把 Ray actor 变成第二套 Agent Runtime；
- AMD GPU 必须验证 `ROCR_VISIBLE_DEVICES`、PyTorch ROCm、失败释放和重复运行稳定性。

退出门禁：功能结果可由现有 verifier 独立复核，并能与 Python 基线进行同口径比较。

### RD3：受控 A/B 与决策

对 Python、Spark 和 Ray 候选路径交错运行至少三次。Ray 只有同时满足下列条件才得到 `GO`：

1. tenant、ACL、训练许可、lineage 和拒绝规则零回归；
2. 三次运行均产生完整、可重放、内容寻址的 evidence；
3. 在目标典型负载上，预先指定的主要指标至少改善 20%，且其他关键指标不退化超过 10%；
4. GPU 阶段没有因调度或传输造成更低的有效利用率；
5. AMD/ROCm 重复运行无资源泄漏、设备误分配或不可恢复崩溃；
6. 运维复杂度和镜像/CVE 增量有明确 owner，并被收益覆盖。

若只在不具代表性的超大 synthetic 数据上获益，结论为 `NO-GO`。

### RD4：Kubernetes 与受治理接入

仅在 RD3 `GO` 后执行：

- 选择临时 `RayJob` 或共享 `RayCluster`，以实际启动延迟和利用率决定，不同时维护两种模式；
- 固定 Ray、Python、ROCm、模型和镜像 digest；
- 将 Job handle、输入 hash、输出 artifact、资源计量和 Ray 运行状态写入现有 run/evidence；
- 配置最小 RBAC、NetworkPolicy、tenant 隔离、超时、取消和清理；
- 失败恢复仍由现有 `JobService`、checkpoint 和 verifier 判定，不信任 Ray Job 自报成功。

退出门禁：隔离环境中完成成功、失败、取消、重试、节点故障和恢复演练。

### RD5：迁移或停止

`GO` 时只迁移已证明受益的批处理阶段。Spark 与 Ray 的双轨观察期必须有结束日期和调用计数；
观察期结束后删除失败路径，避免永久维护两套分布式引擎。

以下任一条件触发停止：

- 普通 Python 已满足目标吞吐和成本；
- Ray 对目标负载没有达到 RD3 收益门槛；
- AMD/ROCm 可靠性不足；
- 需要长期同时维护 Spark、Ray 和重复治理逻辑；
- Ray 只能改善局部 microbenchmark，端到端任务没有收益。

## 5. 最终决策形式

评估结束必须生成不可变 decision receipt，至少包含：

- 输入数据 manifest 与许可范围；
- Python、Spark、Ray 的版本、镜像 digest 和资源配置；
- 三次交错运行的原始指标和 artifact hash；
- verifier 结论、安全/权限回归结果；
- `GO`、`NO-GO` 或 `BLOCKED`，以及迁移范围和回滚目标。

在该 receipt 得到 `GO` 之前，README、架构图和发布材料不得声明 Ray Data 是当前能力。
