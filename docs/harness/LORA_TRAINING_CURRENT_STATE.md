# DataAlchemy LoRA 训练现状与 QLoRA 演进决策

> 状态复核：2026-08-28。本文记录当前代码、本地 adapter 产物和 v3 synthetic
> 发布证据；不把 engineering GO 描述为生产验收。

## 1. 结论

DataAlchemy 当前使用 **FP16 base + Hugging Face PEFT LoRA**，不是 QLoRA。已保存的
TinyLlama 与 Qwen adapter 均使用 `r=16`、`lora_alpha=32`、`dropout=0.05`，覆盖
attention 和 MLP 中的七类主要线性投影。当前发布候选为 TinyLlama adapter
`55365867-b1cc-5899-bca3-1e99f5b923f5`，三次冻结 holdout 均为 98/100。

当前不应把 QLoRA 加入主训练路径：1.1B 和 0.5B base 在现有 96 GiB ROCm
可见内存下没有显存压力，而 bitsandbytes ROCm 后端仍是 preview。应先修复
LoRA 超参数在 training context、manifest 和 worker 之间的单一事实来源；只有当
7B+ target model、长上下文或批量实测出现显存瓶颈时，才启动隔离 QLoRA PoC。

## 2. LoRA 在学习闭环中的位置

```text
Task + Environment + Verifier
              ↓
        governed Experience
              ↓
       Experience Compiler
              ↓
 completion-only SFT snapshot
              ↓
          LoRA GPU Job
              ↓
       candidate adapter
              ↓
 validation + repeated holdout A/B
              ↓
 verified → shadow → canary → promoted/rollback
```

LoRA 是由 Task、Environment、Verifier、Experience 和 label 派生的模型相关产物，不是
长期权威资产。事实知识仍由 RAG 与引用提供；LoRA 主要学习经审核的回答行为、
术语和输出模式，不应替代可更新、可撤销的检索事实。

项目中只有 Model C 进入 LoRA 路径：

| 模型 | 职责 | LoRA |
| --- | --- | --- |
| Model A / DeepSeek | 数据精炼、synthetic Judge | 否 |
| Model B / BGE | embedding 与 reranking | 否 |
| Model C / TinyLlama、Qwen 诊断候选 | SFT、adapter 评测与本地推理 | 是 |
| Model D / DeepSeek | 显式云增强下的最终融合 | 否 |

## 3. 当前实装参数

权重更新可简化为：

\[
W' = W + \frac{\alpha}{r}BA
\]

| 参数 | 当前值 | 说明 |
| --- | ---: | --- |
| `r` | 16 | LoRA 低秩维度 |
| `lora_alpha` | 32 | LoRA 缩放因子 |
| `alpha / r` | 2 | 当前 PEFT 非 RSLoRA scaling |
| `lora_dropout` | 0.05 | LoRA 分支训练 dropout |
| `bias` | `none` | 不训练 base bias |
| `task_type` | `CAUSAL_LM` | 自回归语言模型 |
| `use_rslora` | false | 未启用 RSLoRA |
| `use_dora` | false | 未启用 DoRA |
| `use_qalora` | false | 未启用 QALoRA；该字段不等于 QLoRA base quantization |
| base load dtype | FP16 | `AutoModelForCausalLM` 以 `torch.float16` 加载 |
| base quantization | 无 | 未使用 4-bit/8-bit 权重加载 |

配置定义在 [`models.yaml`](../../models.yaml)，worker 在 [`src/train.py`](../../src/train.py)
中构造 `LoraConfig`。最终 adapter 的 `adapter_config.json` 与上表一致。

### 3.1 Target modules

```yaml
target_modules:
  - q_proj
  - k_proj
  - v_proj
  - o_proj
  - gate_proj
  - up_proj
  - down_proj
```

| 模块 | 子系统 | 作用 |
| --- | --- | --- |
| `q_proj` | Attention | Query 投影 |
| `k_proj` | Attention | Key 投影 |
| `v_proj` | Attention | Value 投影 |
| `o_proj` | Attention | Attention 输出投影 |
| `gate_proj` | MLP | 门控分支 |
| `up_proj` | MLP | 升维分支 |
| `down_proj` | MLP | 降维输出 |

对当前 Llama/Qwen 架构，这等价于覆盖 transformer block 中的主要线性层，
但不包括 embedding 和 `lm_head`。它比只训练 `q_proj/v_proj` 的轻量配置更有
表达能力，也会生成更大 adapter。

## 4. 已训练 target model 与 adapter

### 4.1 TinyLlama：当前发布候选

| 属性 | 值 |
| --- | --- |
| Base path | `/app/data/models/TinyLlama` |
| 架构 | `LlamaForCausalLM` |
| 参数规模 | 约 1.1B |
| Layers | 22 |
| Hidden / intermediate | 2048 / 5632 |
| Attention / KV heads | 32 / 4 |
| LoRA trainable parameters | 12,615,680 |
| FP32 adapter tensor 大小 | 约 48.1 MiB；实际 safetensors 约 49 MiB |
| 已发布 adapter ID | `55365867-b1cc-5899-bca3-1e99f5b923f5` |
| v3 validation | base 20/44，candidate 44/44 |
| v3 repeated holdout | base 38/37/37，candidate 98/98/98 |

12.6M LoRA 参数约占 1.1B base 的 1.1%。运行时由
[`ModelManager`](../../src/inference/model_manager.py) 以 FP16 加载 base，再通过
`PeftModel.from_pretrained` 加载精确 adapter 目录。

### 4.2 Qwen2.5-0.5B-Instruct：诊断 target

| 属性 | 值 |
| --- | --- |
| Base path | `/app/data/models/Qwen2.5-0.5B-Instruct` |
| 架构 | `Qwen2ForCausalLM` |
| 参数规模 | 约 0.5B |
| Layers | 24 |
| Hidden / intermediate | 896 / 4864 |
| Attention / KV heads | 14 / 2 |
| LoRA trainable parameters | 8,798,208 |
| FP32 adapter tensor 大小 | 约 33.6 MiB；实际 safetensors 约 34 MiB |
| v3 validation | 4/44 |
| 发布状态 | 未选中，未进入正式 holdout |

Qwen 产物证明同一 Experience 路径可以面向不同 base 重新编译与训练，但不证明
同一组 `r/alpha/LR/steps` 对不同架构都是最优。

## 5. 训练语义

| 训练项 | 当前值 |
| --- | --- |
| Dataset | MinIO/S3 streaming JSON |
| Train/validation | 从不可变 snapshot 按 `split` 分离 |
| Loss | harness v7 只计算 assistant completion token |
| Max sequence length | 默认 512 |
| Per-device batch | 默认 4 |
| Gradient accumulation | 1 |
| Learning rate | `3e-4` |
| Scheduler | cosine |
| Warmup | 默认 5 steps |
| Weight decay | 0.01 |
| Max steps | 默认 50，可由 Job 环境变量覆盖 |
| Evaluation/save interval | 默认 5 steps |
| Checkpoint selection | 最低 validation loss |
| Precision | Trainer FP16 |
| Artifact | PEFT `adapter_model.safetensors` + `adapter_config.json` |

completion-only 训练会把 system/user/tool/evidence prompt token 的 label 设为 `-100`，
只让经审核的 assistant completion 参与 loss。holdout 不进入 compiler 或 snapshot。

GPU Job 在训练前检查 approved snapshot、base evaluation、compile manifest、tenant 与产物
hash；训练后扫描 safetensors 的非有限数，记录 steps、tokens、GPU、peak VRAM、wall time
和内容哈希。训练完成只产生 candidate，不直接发布。

## 6. 当前主要缺口

### 6.1 LoRA 超参数尚未由 training context 冻结

`src/train.py` 实际从全局 `models.yaml` 读取 LoRA 配置；但 adapter manifest 记录的是
`training_context.get("lora_config", {})`，而当前 harness v7 compiled training context 没有强制保存
`lora_config`。这造成潜在的证据错位：

- worker 实际使用的参数可能来自后来被修改的全局文件；
- manifest 可能没有记录实际 `r/alpha/targets`；
- 相同 snapshot 未必能仅凭已保存 context 重放训练。

修复时应将 algorithm、rank、alpha、dropout、解析后 target modules、dtype、learning rate、
batch、accumulation、steps 和 seed 全部写入 approved training context。worker 只能使用这份冻结
context，manifest 和 verifier 复核同一份配置。

### 6.2 没有完成 model-specific 超参数搜索

设计文档已要求小规模比较 learning rate/rank/epoch，但当前本地 adapter 仍全部是
`r=16 / alpha=32 / dropout=0.05`，且 `learning_rate=3e-4` 仍硬编码。现有证据只证明
该组参数对已选 TinyLlama candidate 有效，不证明它对 TinyLlama 最优，更不证明
它对 Qwen 最优。

### 6.3 dtype 和 target modules 仍有全局配置耦合

`models.yaml` 声明 `dtype: float16`，worker 又硬编码 `torch.float16`。当前两者结果一致，
但不是单一事实来源。七个 target module 名称对 Llama/Qwen 均有效，换架构前仍须验证
模块覆盖，并将解析后的精确列表冻结到 manifest。

## 7. ROCm 与 QLoRA 现状

QLoRA 通常把冻结 base 以 4-bit NF4/FP4 加载，并对 LoRA 参数保持较高精度训练。
它的主要价值是减少训练时 base weights 的显存占用，不保证提高准确率，也不会在
线上仍加载 FP16 base 时自动降低部署显存。

“ROCm 不支持 QLoRA”已不是准确的当前结论：

- bitsandbytes 已提供 AMD ROCm backend，但仍标记为 preview；
- 当前官方 wheel 矩阵包含 Linux x86-64、ROCm 7.1 和 `gfx1151`；
- 官方文档声明 4-bit/NF4/FP4 功能可用；
- Windows ROCm bitsandbytes 尚不在当前支持范围内。

参考：

- [bitsandbytes AMD ROCm 安装与支持矩阵](https://huggingface.co/docs/bitsandbytes/en/installation)；
- [AMD: Fine-tuning Llama-3.1 with QLoRA](https://rocm.docs.amd.com/projects/ai-developer-hub/en/v3.1/notebooks/fine_tune/QLoRA_Llama-3.1.html)；
- [PEFT LoRA/QLoRA-style target modules](https://huggingface.co/docs/peft/package_reference/lora)。

2026-08-28 本地实测环境：

| 项 | 观测值 |
| --- | --- |
| PyTorch | `2.9.1+rocm7.1.0` |
| HIP runtime | 7.1 |
| GPU | Radeon 8060S Graphics |
| GCN arch | `gfx1151` |
| PyTorch 可见内存 | 96 GiB |
| bitsandbytes | 未安装 |

因此，当前硬件与 ROCm 版本已具备 QLoRA PoC 的前提，但项目还没有实装、验证
或发布 QLoRA 路径。

## 8. 是否引入 QLoRA

### 8.1 当前决策：不进入主路径

| 考量 | 当前判断 |
| --- | --- |
| TinyLlama 1.1B FP16 weights | 约 2.2 GB，远低于当前可用内存 |
| Qwen 0.5B FP16 weights | 约 1 GB，没有训练存储压力 |
| 当前准确率 | TinyLlama candidate 已达 98/100，QLoRA 不是质量缺口的对应解法 |
| 增量复杂度 | 需新增 bitsandbytes、4-bit config、kernel preflight、fingerprint 和 verifier |
| ROCm 成熟度 | 可用但仍是 preview，需要实机性能与稳定性证据 |

在现有模型规模下，QLoRA 只会节省约 1--2 GB base weight 内存，不足以抵消新增
的运维和验证成本。

### 8.2 启动 QLoRA PoC 的条件

任一条件实测成立时，才建立隔离 PoC：

1. target model 扩展到 7B 或以上；
2. FP16 LoRA peak VRAM 超过可用内存的 80%；
3. 目标 batch/context length 在 FP16 LoRA 下 OOM；
4. 需要在同一 GPU 上保留多个训练/评测候选；
5. 受控 A/B 证明更大 base 能解决现有小模型无法解决的能力缺口。

理论上仅计 base weights 的内存对比，不包括 activation、LoRA、optimizer 和 runtime
workspace：

| Base 规模 | FP16 | 4-bit |
| ---: | ---: | ---: |
| 1.1B | 约 2.2 GB | 约 0.55 GB |
| 7B | 约 14 GB | 约 3.5 GB |
| 14B | 约 28 GB | 约 7 GB |
| 32B | 约 64 GB | 约 16 GB |

## 9. 后续工作顺序

### P0：先修复标准 LoRA 可复现性

1. 将全部 LoRA 与训练超参数写入 approved training context；
2. `validate_training_context` 对参数、范围和 target module 进行 fail-closed 校验；
3. worker 禁止从全局 `models.yaml` 回退读取已审批训练的参数；
4. adapter manifest、cost receipt 和 verifier 记录同一 training config digest；
5. 对已有旧 manifest 保持只读兼容，不伪造缺失超参数。

### P1：最小 model-specific LoRA 比较

在不触碰 holdout 的前提下，仅用 validation 比较小型参数网格：

- `r ∈ {8, 16, 32}`；
- alpha 初始保持约 `2r`；
- 少量 learning rate/steps 组合；
- 同一 dataset、seed、prompt transform 和 generation policy。

只在 validation 明确胜出的候选上执行冻结 repeated holdout。

### P2：条件性 QLoRA PoC

如果 7B+ 或显存条件触发，在独立训练镜像中固定 bitsandbytes/ROCm/PyTorch 组合，
并将以下字段加入 training context 和 fingerprint：

- `algorithm=qlora`；
- `load_in_4bit`；
- `bnb_4bit_quant_type`；
- `bnb_4bit_compute_dtype`；
- `bnb_4bit_use_double_quant`；
- bitsandbytes、PEFT、Transformers、PyTorch、ROCm 精确版本；
- GPU architecture 与量化 kernel preflight 结果。

同数据、seed、rank、alpha、targets 和 steps 对比 FP16 LoRA/QLoRA，至少记录 validation、
holdout、peak VRAM、tokens/s、wall time、数值异常与三次重复稳定性。只有在显存
明显下降、质量不超出预设回退且 verifier 可重放时，才允许进入主路径。

## 10. 当前决策摘要

| 事项 | 决策 |
| --- | --- |
| 当前训练算法 | FP16 PEFT LoRA |
| 当前标准配置 | `r=16 / alpha=32 / dropout=0.05 / seven projection targets` |
| 当前发布 target | TinyLlama 1.1B |
| Qwen2.5-0.5B | 诊断 target，未发布 |
| 现在引入 QLoRA | 否 |
| 首要工作 | 冻结并验证 model-specific LoRA/training config |
| QLoRA 触发条件 | 7B+、OOM、peak VRAM > 80% 或可证明的并行训练需求 |
| QLoRA 定位 | 可退出的隔离 PoC，不替换已验证 LoRA 路径 |
