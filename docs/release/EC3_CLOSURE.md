# EC3 本地真实 LLM judge 关闭记录

2026-09-22：EC3 在最终运行时候选
`5e9c1839406251ab7e311c0b7ea3e17e666d380f` 上以 **synthetic engineering PASS** 关闭。
它不是人工校准、真实业务质量或发布批准。

## 最终结果

- judge：本地 `Qwen/Qwen2.5-7B-Instruct`；候选是确定性 extractive 路径，judge 与候选不同族；
  无云外发、费用为 0。
- calibration：100 个正例全部接受，100 个负例全部拒绝，200/200 可判定、0 invalid；报告
  `cb7cb88f…98a62`。
- 冻结 v2 holdout：100 case，三轮均 100/100，0 invalid、0 critical failure；报告
  `2b138fb8…e442`。
- 调用预算：calibration 200 次、holdout 300 次，共 163,960 input token、1,000 output token；低于
  1,200 calls / 12,288,000 token 冻结上限，无重试。
- 完整 report/audit 以内容哈希条件写入
  `ec4-evidence/ec3/5e9c1839406251ab7e311c0b7ea3e17e666d380f/`；机器可读摘要见
  [EC3_CLOSURE_EVIDENCE.json](EC3_CLOSURE_EVIDENCE.json)。纯代码重放已重新生成候选输出并核对
  judgment、指标和最终 decision。

## 失败历史与候选演进

Qwen2.5-3B 首次 calibration 达到下限，但 v1 holdout 三轮均为 87/100，EC3 判定 NO_GO；失败证据
不可变保留。随后保持 prompt 与候选不变，升级并重新校准 Qwen2.5-7B，并轮换为未暴露的 v2
holdout，原失败没有被覆盖。

早期候选 `6f8799f…` 的 EC3 曾通过；之后 EC5 暴露 Q4 回放的引用选择和 verifier 权限/抽取缺陷，
因此不能沿用其结论。修复经过 `6704dd7…`、`25c5b9b…`，最终在 `5e9c183…` 上重新执行完整
calibration、三轮 holdout 与重放，以上结果只对应最终候选。

## 边界

所有 case 都是项目生成的公开 synthetic 模板，模板相关且不代表业务分布；`human_reviewed=false`、
`judge_only=true`、`independent_semantic_verification=false` 保持不变。该 PASS 只允许关闭 EC3 工程
门禁，不替代真实数据、人工 reviewer、生产 OIDC、真实流量或业务试点。
