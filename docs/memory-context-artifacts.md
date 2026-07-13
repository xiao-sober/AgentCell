# 记忆、上下文压缩与 Artifact

本文说明阶段 7 的四层记忆、作用域检索、上下文压缩和 Artifact 持久化边界。实现入口位于 `src/agentcell/memory/`、`src/agentcell/storage/artifact_store.py` 和 Alembic revision `20260713_0003`。

## 1. 四层记忆

| 类型 | 用途 | 典型生命周期 |
| --- | --- | --- |
| Working | 当前 Run 的计划、阶段和临时事实 | 短期，可设置过期时间 |
| Conversation | 会话消息与工具历史的可检索表示 | 会话级 |
| Episodic | 一次任务的结果、决策和经验摘要 | 项目级长期记忆 |
| Semantic | 稳定偏好、规则和项目事实 | 长期；写入必须显式批准 |

每条记忆都绑定 `user_id + project_id + agent_id`。检索必须匹配用户和项目；Agent 级查询可以读取同一 Agent 及 `agent_id=NULL` 的项目公共记忆，不能跨用户或跨项目读取。

## 2. 写入策略

所有写入经过 `MemoryPolicy`：

- 内容规范化后以 SHA-256 在相同类型和作用域内去重；
- 私钥、API Key、密码和 Authorization 等凭据特征默认拒绝持久化；
- Semantic 记忆必须由调用方传入显式批准结果；
- `expires_at` 到期的记录不会进入召回结果；
- 更新和删除都必须携带完全相同的作用域，避免只凭 UUID 越权操作。

阶段 7 提供应用服务级 CRUD。审批结果由上层调用方显式传入；阶段 9 的 HTTP/CLI 产品接口需要把它接到统一审批交互，不能自行创建永久全局批准。

## 3. 检索与排序

SQLite `memory_fts` 使用 FTS5 `unicode61` 分词器，并由 INSERT、UPDATE、DELETE 触发器与 `memory_items` 同步。用户查询先转换成安全的词元表达式，再取 BM25 候选。

最终分数由以下部分组成：

```text
0.55 × BM25 归一相关度
+ 0.20 × importance
+ 0.20 × 30 天半衰期时间衰减
+ 0.05 × 查询标签重合度
```

过期记录先过滤，再排序和截断。首版不依赖向量数据库。

## 4. 上下文处理

每次 Provider 请求前，`ContextManager` 依次执行：

1. `MemoryInjector`：把作用域内的相关记忆作为“仅供参考、不是指令”的 System Prompt 注入；
2. `PairSafeTrimmer`：裁剪历史时扩展保留集合，确保 ToolCall 和 ToolReturn 不会只留下单边；
3. `ToolOutputCompactor`：超过内联上限的 ToolReturn 写入 Artifact，消息中只保留摘要和引用；
4. 内容发生裁剪或外置时记录 `context.compacted`，召回记忆时记录 `memory.recalled`。

`EpisodicSummarizer` 使用独立 `model_ref`，固定低温度并拒绝启用深度思考的模型配置。它返回 `MemoryCandidate`，仍需经过 Memory Policy 才能持久化。

## 5. Artifact Store

`FileArtifactStore` 将内容写入受控根目录，数据库只保存元数据：媒体类型、字节数、SHA-256、存储键、建议名称和 UTC 创建时间。

安全与恢复规则：

- 单 Artifact 默认最大 64 MiB，写入前检查预算；
- 建议文件名被清洗，实际路径由随机 UUID 生成，不能由调用方控制；
- 使用同目录临时文件后原子替换；
- 相同哈希和大小的内容复用已有 Artifact；
- 每次加载都重新校验文件大小和 SHA-256；
- 存储键解析后必须仍位于 Artifact 根目录；
- 检查点保存消息内的 Artifact UUID，重启后可由数据库元数据和文件内容重新加载。

事件和模型上下文只保存 `ArtifactReference`，不直接内联大型内容。

## 6. 数据库迁移

应用不会在启动时创建这些结构。首次拉取阶段 7 代码后执行：

```powershell
uv run alembic upgrade head
```

revision `20260713_0003` 创建 `artifacts`、`memory_items`、`memory_fts` 和三个 FTS 同步触发器。降级会先删除触发器与虚拟表，再删除普通表。

## 7. 当前边界

阶段 7 没有开放写文件、删除、Shell 或 HTTP 工具。它只提供这些生产工具所依赖的 Artifact、记忆与上下文基础。危险工具将在阶段 7.1 通过既有 `ToolExecutor → CapabilityLease → Approval → Checkpoint` 路径实现。
