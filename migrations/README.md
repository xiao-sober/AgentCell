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

## 命令

```powershell
uv run alembic upgrade head
uv run alembic current
uv run alembic downgrade base
```

默认数据库为 `.agentcell/agentcell.db`，可通过 `AGENTCELL_DATABASE_URL` 覆盖。URL 必须使用 `sqlite+aiosqlite`。

测试从独立临时数据库执行升级和降级，并验证 WAL、外键、5000ms busy timeout、只追加触发器、FTS5 同步触发器和并发事件 sequence。
