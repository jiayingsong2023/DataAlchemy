# PDF 到问答、记忆和 LoRA 的最小闭环

这份文档说明如何把一份 PDF 接入 DataAlchemy，并在 WebUI 中验证内容是否被利用。

先说明当前边界：

- PDF → raw → Spark rough clean → fine clean → PostgreSQL documents/chunks → RAG → WebUI 问答，当前可以通过单文件试点入口完成。
- 记忆写入来自**会话中的提炼结果**，不是把外部 PDF 原文直接写进长期记忆；低风险个人记忆可以自动批准，团队/租户记忆仍需审核。
- PDF 上传不会自动触发 LoRA。LoRA 还需要 H5 的标注、训练快照、GPU Job、固定评测和发布审批；当前没有“一次上传后 WebUI 自动训练 adapter”的入口。

## 1. 准备 PDF 和环境

PDF 必须是可复制文本、未加密、损坏检查通过，默认不超过 25 MiB。扫描件 OCR、复杂表格和密码保护 PDF 当前会被拒绝。

启动 PostgreSQL/pgvector、MinIO、Redis、WebUI 和 k3d Job 环境后，确认：

```bash
.venv/bin/python scripts/pilot_check.py
kubectl get pods -n data-alchemy
```

生产环境使用 OIDC；本地试点使用管理员账号即可。

## 2. PDF 入库：rough clean → fine clean → RAG

1. 登录 WebUI，进入 **Agent Tasks**，点击文件导入，选择 PDF。
2. 输入一个能验证文档内容的问题，例如：

   ```text
   这份文档的支持时间和 P1 处理要求是什么？
   ```

3. 按页面提示逐步批准当前 tenant、当前 PDF hash 对应的任务。
4. 在任务详情中等待以下五步全部通过：

   | 阶段 | 主要产物 |
   | --- | --- |
   | Input validation | PDF SHA-256、source version、ACL snapshot |
   | Spark rough clean | MinIO `cleaned_corpus`、accepted/quarantined/rejected 计数 |
   | Fine clean / refine | normalized corpus、页码 locator、PII/injection 检查 |
   | Publish | PostgreSQL `documents`、`document_chunks`、ACL、FTS/向量 |
   | RAG probe | 固定问题命中的 chunk、source version、PDF 页码引用 |

原始 PDF 只保存在受限 MinIO raw 区；检索权威是 PostgreSQL，不是 MinIO 文件本身。

## 3. 让会话内容进入 memory system

PDF 内容先通过 RAG 被回答，再从会话记录提炼记忆：

1. 创建聊天会话时开启 `auto_memory_enabled`，或在 WebUI 的会话设置中打开自动记忆。
2. 针对 PDF 提问并确认回答引用了正确页码。
3. 结束会话，或调用：

   ```bash
   curl -X POST "$WEBUI_URL/api/sessions/$SESSION_ID/distill" \
     -H "Authorization: Bearer $TOKEN"
   ```

4. 查看 `/api/memories?query=关键词` 或 WebUI 的 Memory 面板：
   - `approved`：可被后续上下文检索；
   - `candidate`：需要审核；
   - `conflicted`/`rejected`：不会作为有效记忆使用。

外部 PDF 默认是 `untrusted_external`。因此不要把 PDF 中的“请调用工具”或“写入长期记忆”等文字当作系统指令；这类内容会被清洗或隔离。

## 4. 用 PDF 相关数据训练 LoRA adapter

这一步目前是受控的 H5 运维流程，不是 PDF 上传任务的自动后续步骤：

1. 从已完成的 PDF 问答轨迹中选择训练样本和 validation 样本。
2. 人工审核 annotation，并明确 `training_allowed`、training purpose 和 permission version。
3. 创建并审核 `training_snapshot`。
4. 在 GPU Job 中执行 LoRA，生成 adapter manifest。
5. 用同一固定 evaluation suite 对 base/candidate 评测。
6. 通过 safety scan、独立 reviewer、shadow/canary 和 rollback 检查后，才允许发布 adapter。

当前可参考 H5 工程演练：

```bash
.venv/bin/python scripts/run_h5_rehearsal.py
```

该脚本使用 synthetic 数据，只能验证 LoRA 工程链路，不能把本次 PDF 自动变成生产 adapter。实际 PDF LoRA 需要通过 H5 evaluation/snapshot/release API 或受控运维脚本接入，不能直接把 PDF 原文喂给训练程序。

## 5. 在 WebUI 验证最终问答

完成入库后，在聊天框重复第 2 步的问题。验收以下内容：

- 回答包含 PDF 的实际内容；
- Evidence 显示正确的 `document_id`、`chunk_id`、source version 和页码；
- `/api/sessions/{session_id}/context` 能看到相关 document chunk 和 approved memory；
- 反馈、会话、记忆和任务都属于当前 tenant；
- 如果 adapter 已通过 H5 发布，模型版本/adapter release 能在 H5 页面中查询，否则问答仍使用 base model + RAG。

## 6. 一句话结论

当前项目可以完整展示 **PDF → rough clean → fine clean → PostgreSQL RAG → WebUI 问答 → 会话记忆提炼**。但 **PDF → 自动 LoRA → adapter 发布** 仍是需要人工审核和 H5 评测门禁的独立流程，尚未提供单击自动闭环。
