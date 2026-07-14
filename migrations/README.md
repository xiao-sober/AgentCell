# 数据库迁移

此目录包含 AgentCell 的 Alembic 环境、迁移模板和版本脚本。生产 Schema 只能通过这里的迁移变更，应用启动不得临时创建或修改生产表。

## 当前迁移

`versions/20260710_0001_create_runs_and_events.py` 创建：

- `runs`：Run 投影、父子关系和原子事件 sequence 计数器；
- `run_events`：版本化 JSON payload 和 Run 内 sequence；
- `(run_id, sequence)` 唯一约束；
- 禁止 `run_events` UPDATE/DELETE 的 SQLite 触发器；
- conversation、parent 和事件时间查询索引。

`versions/20260713_0002_add_approvals_checkpoints.py` 创建：

- `approvals`：完整审批影响信息、参数和单次决策状态；
- `checkpoints`：消息历史、预算、租约、待审批调用及分支来源；
- `tool_executions`：按 `(run_id, provider_call_id)` 去重的持久化执行账本；
- Run/状态查询索引和必要的外键、唯一约束及状态检查约束。

`versions/20260713_0003_add_memory_artifacts.py` 创建：

- `artifacts`：文件 Artifact 的媒体类型、大小、SHA-256、受控存储键和 UTC 元数据；
- `memory_items`：四层记忆、用户/项目/Agent 作用域、标签、重要度、过期时间和内容去重哈希；
- `memory_fts`：使用 `unicode61` 的 SQLite FTS5 索引；
- 三个同步触发器，确保记忆新增、编辑和删除同步更新 FTS5；
- Artifact 内容哈希/大小、存储键和记忆作用域等约束及索引。

`versions/20260713_0004_add_agent_delegations.py` 创建：

- `agent_delegations`：区分分支 lineage 与真实父子 Agent 关系；
- 持久化 provider tool-call、子 Run、目标 Agent、深度、租约、分配预算、实际 Usage 和结构化结果；
- 对 `(parent_run_id, provider_call_id)` 和 `child_run_id` 建立唯一约束；
- 提供父 Run/状态查询索引，支持取消传播和重启恢复。

`versions/20260713_0005_add_agents.py` 创建：

- `agents`：持久化通过 HTTP API 或 CLI 创建、更新的无状态 Agent 定义；
- 内置 Agent 仍由代码提供，持久化同 ID 定义会在启动时覆盖内置默认值；
- 运行时状态、预算和工作区不会写入 Agent 定义表。

`versions/20260714_0006_add_conversations.py` 创建：

- `conversations`：固定用户、项目、工作区和 Agent 作用域，并用 `active_run_id` 原子限制单个活动根 Run；
- `messages`：Conversation 内单调 sequence、Run 关联、请求/响应类型、版本化脱敏 payload 和 Artifact 引用；
- 旧 Run 不会被静默回填为可续聊 Conversation，仍可继续 inspect、replay 和 branch。

`versions/20260714_0007_add_file_changes.py` 创建：

- `change_sets`：每个 Run 一个文件变更集合，保存 Conversation、Agent、工作区和可选 Git 基线；
- `file_changes`：保存 created/replaced/patched/deleted/reverted 状态、before/after 哈希与 Artifact、完整 Diff、审批和 Provider tool-call 关联；
- `prepared/applied/completed/conflict/failed/reverted` 恢复状态和 Run/ChangeSet 时间顺序索引；
- 文件内容仍位于受哈希校验的 Artifact Store，数据库只保存稳定引用和恢复投影。

## 命令

```powershell
uv run alembic upgrade head
uv run alembic current
uv run alembic downgrade base
```

默认数据库为 `.agentcell/agentcell.db`，可通过 `AGENTCELL_DATABASE_URL` 覆盖。URL 必须使用 `sqlite+aiosqlite`。

测试从独立临时数据库执行升级和降级，并验证 WAL、外键、5000ms busy timeout、只追加触发器、FTS5 同步触发器、Run 事件 sequence 和 Conversation 消息 sequence。
