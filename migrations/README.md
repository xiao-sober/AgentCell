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

`versions/20260715_0008_add_run_execution_identity.py` 增加：

- `runs.execution_identity`：保存版本化 AgentSpec 与 ModelSpec 快照及各自 SHA-256，用于恢复、审批和分支时校验不可变执行身份；
- 字段保持 nullable 以允许既有数据库完成迁移，但缺少快照的旧 Run 不得继续模型执行或审批恢复，必须以 `run_identity_mismatch` 安全失败；
- 降级只移除该列，不改写既有事件、审批、检查点或文件变更历史。

`versions/20260715_0009_bind_conversation_model.py` 增加：

- `conversations.model_ref`：新 Conversation 固定其配置模型，后续回合不得随进程默认模型或 TOML 顺序漂移；
- 既有 Conversation 优先从已完成 Run 的 `execution_identity.model_ref` 回填，再回退到持久化 AgentSpec；无法可靠推断的空历史旧会话保持未绑定，并要求用户显式选择一次模型；
- 回填直接读取身份 JSON，不要求历史 v1 capability 哈希先通过当前进程顺序校验。

## 命令

```powershell
uv run alembic upgrade head
uv run alembic current
uv run alembic downgrade base
```

默认数据库为 `.agentcell/agentcell.db`，可通过 `AGENTCELL_DATABASE_URL` 覆盖。URL 必须使用 `sqlite+aiosqlite`。

测试从独立临时数据库执行升级、降级和 Alembic metadata check，并验证 WAL、外键、5000ms busy timeout、只追加触发器、FTS5 同步触发器、Run 事件 sequence、Conversation 消息 sequence、执行身份列和 Conversation 模型回填。

阶段 9.2.2 主体没有新增持久化结构；续聊正确性补丁增加上述 Conversation 模型绑定列，当前迁移头为 `20260715_0009`。Agent 的 builtin/persisted/override 来源和 public/internal 可见性仍由应用组合确定，不写入 AgentSpec。

阶段 9.3 没有新增数据库表或列。版本化 TeamSpec 由应用注册；每次实际 Team 执行继续使用既有 `runs`、`run_events`、`checkpoints` 和 `agent_delegations` 保存 root/child 状态、阶段身份、预算、租约、审批与恢复数据。该阶段结束时迁移头为 `20260715_0009`。

阶段 9.4 的权威 Task Router 继续复用 `runs`、`run_events`、`checkpoints` 与 `agent_delegations`。迁移 `20260716_0010` 为 `conversations` 增加 `routing_mode`、`team_id` 和 `routing_policy_version`：既有记录以 `fixed` 回填，auto Conversation 绑定 `task-router` 与版本化 RoutingPolicy。当前迁移头为 `20260716_0010`。
