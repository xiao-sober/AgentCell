# 数据库迁移

此目录包含 AgentCell 的 Alembic 环境、迁移模板和版本脚本。生产 Schema 只能通过这里的迁移变更，应用启动不得临时创建或修改生产表。

## 当前迁移

`versions/20260710_0001_create_runs_and_events.py` 创建：

- `runs`：Run 投影、父子关系和原子事件 sequence 计数器；
- `run_events`：版本化 JSON payload 和 Run 内 sequence；
- `(run_id, sequence)` 唯一约束；
- 禁止 `run_events` UPDATE/DELETE 的 SQLite 触发器；
- conversation、parent 和事件时间查询索引。

## 命令

```powershell
uv run alembic upgrade head
uv run alembic current
uv run alembic downgrade base
```

默认数据库为 `.agentcell/agentcell.db`，可通过 `AGENTCELL_DATABASE_URL` 覆盖。URL 必须使用 `sqlite+aiosqlite`。

测试从独立临时数据库执行升级和降级，并验证 WAL、外键、5000ms busy timeout、只追加触发器和并发事件 sequence。
