# EC5 集成交付关闭记录

2026-09-22：EC5 在固定运行时候选
`5e9c1839406251ab7e311c0b7ea3e17e666d380f` 上以 **synthetic engineering PASS** 关闭。
当前交付状态是“等待业务验收、工程证据完整”，不是生产发布或 GA。

## 同一候选证据

| 检查 | 结果 |
| --- | --- |
| EC3 本地真实 judge | calibration 100/100 正例、0/100 负例误接受；v2 holdout 三轮 100/100 |
| RAG 投影 A/B | 两臂 Recall@5 1.0、MRR 0.928571、coverage 1.0；candidate citation precision 0.257143 |
| 直接运行时容量 | 并发 1/4 均 21/21；p95 1530.814/5892.687 ms；PASS |
| HTTP 部署路径 | 并发 1/4 均 21/21、0 error；p95 1975.664/8293.197 ms；PASS |
| 真实浏览器 | Chrome 153 WebSocket 主路径返回“循着酒香”、`原文抽取`、p.3 引用；PASS |
| 故障恢复 | 删除 active WebUI Pod 后自动恢复；同一 request_id 返回相同 task/run/answer/citations；PASS |
| 回滚/前滚 | 回滚到 `25c5b9b…` 健康，再前滚到 `5e9c183…` 健康；PASS |
| 代码回归 | 409 passed / 41 skipped / 0 failed；Ruff 全库通过 |

机器可读的 digest、request/run 标识和性能指标见
[EC5_CLOSURE_EVIDENCE.json](EC5_CLOSURE_EVIDENCE.json)。运行时报告以内容哈希保存在 MinIO；数据库、
对象存储和 Kubernetes 是执行证据源，仓库 JSON 是脱敏聚合索引。

## 保留的失败与修复链

1. `6f8799f…` 的直接容量检查为 NO-GO：candidate 质量 0/21。
2. `6704dd7…` 仍为 NO-GO：相同证据副本导致唯一引用选择失败。
3. `25c5b9b…` 的 HTTP 路径全部返回 409：verifier 缺少 `document_acl` 读取权限，且暴露抽取契约不严。
4. `5e9c183…` 加入最小 ACL migration，并要求 Q4 answer 与 citation quote 精确一致；随后重新执行
   EC3、直接容量、HTTP、浏览器、恢复和回滚，全部通过。失败 receipt 保留，不被最终 PASS 覆盖。

## 复用与限制

EC4 的创建端、worker、审批、配置/输入绑定和独立 verifier 证据继续有效；从 EC4 固定点到最终候选的
源码差异仅涉及回答路径及 verifier ACL migration，不改变训练配置或 worker。最终 Web、Inference 与
Harness 镜像由同一候选链的不可变镜像替换 `/app/src` 后生成并核对 imageID；这足以绑定本轮运行证据，
但不是最终候选的 clean supply-chain rebuild。HTTP 压测 driver 因 harness 镜像缺少 Web auth 依赖，使用
同一 SHA Web 镜像中的依赖生成独立测试镜像；不把该测试镜像声明为交付运行时。

## 仍然阻塞发布的外部门禁

- 授权且具代表性的真实业务数据与 RTD-Q5 manifest；
- 独立人工校准、失败 case 复核和业务 owner 签署；
- 目标生产 IdP 的 OIDC/tenant/role 联调；
- 真实 stable/candidate 流量、两支团队四周试点和 GA-01。

因此候选可以进入业务验收，不能标记 `PILOT_READY`、production ready 或 GA。LLM-as-judge 只关闭
synthetic engineering gate，不替代人工决策。
