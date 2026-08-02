# H3 退出报告：可验证产品闭环

> 状态：发布候选通过。真实团队四周试点、H4 记忆、H5 训练/LoRA/发布仍未关闭。

## 1. 结论

H3 的核心门禁已通过：一个 strict `run_id` 能从受控 DOCX 输入，经过真实 Kubernetes Spark
rough-clean、独立 verifier、确定性 refine、PostgreSQL/pgvector 发布和带 chunk 引用的 RAG，
最终发布不可变 MinIO evidence manifest。PDF 也完成了真实 k3d Spark Job 验收。

## 2. 真实证据

| 项目 | 结果 |
| --- | --- |
| 分支 | `feat/harness-h3-product-loop` |
| Spark 镜像 | `data-alchemy-harness:h3`，`sha256:155b5239d3f7639c2bf631787b8685801da6859870b406d3b15afe22858279ba` |
| DOCX strict run | `run_id=ee2a6cf7-8669-4b55-a28d-5f13c08c9389`，task `43b09a95-c7d5-4926-b959-19b8f1434209` |
| DOCX 最终状态 | `succeeded / verified_evidence_published` |
| DOCX Spark Job | `job_id=86fe2a3d-def4-40b7-9333-0bd632c3e5fc`，Pod `da-ee2a6cf7-4808ff57-a1-vjx8x`，`Succeeded` |
| DOCX 输入 SHA | `99e42fc8e6893510d6bfeeb2db520443f4ea394c0b7ebeec0e132f941daca72a` |
| PDF Spark Job | `h3-real-pdf` / Pod `h3-real-pdf-2jsqs`，`Succeeded`，输入 SHA `ae9ca267d98983de7fa4f13f8d0c1a22a2c3da68a89306ad11eb4f9923076df6` |
| 发布 document | `4a202cdf-99cc-43b7-943f-bf8431399694`，3 chunks，`ready` |
| evidence manifest | `evidence/pilot/ee2a6cf7-8669-4b55-a28d-5f13c08c9389/manifests/sha256/01939ae4b287c92957d7e9e164a469b7091f663f172743f40ca6c4eaaa062291.json` |
| manifest SHA | `01939ae4b287c92957d7e9e164a469b7091f663f172743f40ca6c4eaaa062291` |

DOCX run 的五个 after-step verifier（input、rough、refine、publish、retrieval）全部为 `passed`；
rough 阶段报告 3 条 accepted、0 条 quarantined/rejected，refine 生成 1 个 document/3 个 chunks；
retrieval 返回 3 个实际 chunk citation，包含 source URI、source version、locator 和 run ID。

## 3. 安全与污染轨迹

- DOCX prompt-injection 固定夹具会在 rough 阶段进入 quarantine，不能进入 normalized corpus。
- 工具计划和 scope 在 TaskSpec 中冻结；没有 `sync_git` 或计划外工具调用。
- refine/publish 只读取本 run 的 content-addressed artifact；发布由 PostgreSQL 事务和独立
  `verify_ingest@2` 复查。
- `compare_sources` 已覆盖同值自动裁决和冲突 `needs_approval` 两条分支；`resolve_conflict`
  只能选择报告已有候选并记录审批人。
- 文档 ACL、tenant、source version、trust label 和 citation 链均在 verifier 中复查。

## 4. 测试结果

- H3/H0--H2 相关套件：`6 passed, 25 skipped`。
- Python 编译、目标范围 Ruff（E9/F）和 `git diff --check`：通过。
- 全量测试：`33 passed, 34 skipped, 1 failed`。唯一失败是已有 Phase 0 配置测试要求外部
  `VERIFIER_DATABASE_URL`，与 H3 代码无关；未以修改测试或默认凭据掩盖。

## 5. 未关闭门禁

- H4：context compaction、memory distillation、冲突记忆治理尚未实现。
- H5：轨迹评测基线、训练快照、LoRA、shadow/canary 和 release 尚未实现。
- H6/GA-01：两支独立真实团队连续四周试点和周度审计必须在外部完成；本地模拟不能替代。
