# FastAPI、AG-UI/SSE 与 CLI

阶段 9 将既有 RunService、ReplayService、MemoryService 和 Registry 暴露为产品接口。FastAPI 与 Typer 都通过 `AgentCellApplication` 组合根调用同一业务服务，不在路由或命令中复制生命周期、审批、权限和预算判断。

## 启动

先升级数据库，再启动服务：

```powershell
uv run alembic upgrade head
uv run agentcell serve --host 127.0.0.1 --port 8000
```

离线开发可使用确定性 Fake Provider：

```powershell
uv run agentcell serve --offline-fake
```

服务读取 `AGENTCELL_CONFIG`、`AGENTCELL_DATABASE_URL` 和 `AGENTCELL_OFFLINE_FAKE`。密钥仍只由 Provider 的环境变量解析，不通过 Provider API、OpenAPI Schema 或错误响应返回。

## HTTP 资源

当前接口位于 `/api`：

- Runs：创建、查询、取消、恢复、分支、审批列表和 AG-UI/SSE 事件；
- Conversations：创建、列表、详情、有序消息和会话内新 Run；
- Approvals：提交批准、拒绝或修改参数后的决定；
- Agents：列出、创建和更新无状态 Agent 定义；
- Memories：按用户/项目/Agent 作用域搜索和删除；
- Providers、Tools：返回脱敏模型信息和工具 Schema/策略；
- System：健康与版本信息。

所有 AgentCell 领域错误映射为 `application/problem+json`。不存在资源返回 404，重复 Agent、冲突审批或非法恢复返回 409，租约拒绝返回 403，请求 Schema 错误返回 422。取消和重复相同审批决定保持幂等；终态 Run 不允许重新恢复。

通过 API 创建或更新的 Agent 定义保存到 `agents` 表。内置 Agent 仍由代码提供，持久化的同 ID 定义会在重启时覆盖内置默认值；Run 状态、租约、预算和工作区不会写入 Agent 定义。

## Conversation 与多轮 Run

`POST /api/runs` 和 `agentcell run` 保持单次任务语义；任意复用裸 `conversation_id` 不会触发历史加载。真实多轮必须先创建持久化 Conversation，再通过 Conversation 的 runs 端点或 `agentcell chat` 创建下一回合。终态 Run 不能追加用户消息，`resume` 仍只用于暂停、审批等待或分支恢复。

当前产品接口：

```text
POST /api/conversations
GET  /api/conversations
GET  /api/conversations/{conversation_id}
GET  /api/conversations/{conversation_id}/messages
POST /api/conversations/{conversation_id}/runs
```

每次用户追问都创建同一 Conversation 下的新 Run。服务端按稳定序号读取有界、成对且已脱敏的历史，执行用户、项目、工作区和 Agent 作用域校验，并拒绝同一 Conversation 中互相冲突的并发活动 Run。上一轮的剩余预算、临时审批和临时扩权不会继承到新 Run。

读取 Conversation、消息或创建回合时必须提供匹配的 `user_id`；不匹配返回 403，同一 Conversation 已有活动根 Run 时返回 409。完整历史保存在 `messages`，只将已完成 Run 的有界、脱敏历史注入新回合。

## AG-UI 与 SSE

`GET /api/runs/{run_id}/events` 从已提交的领域事件构建官方 AG-UI 事件。它不会把数据库 payload 直接作为前端协议，也不会把 AG-UI 当成新的事件源。

一个领域事件可能映射为多个 AG-UI 事件，因此 SSE ID 使用复合游标：

```text
<domain sequence>.<mapped event offset>
```

客户端重连时传入 `Last-Event-ID`。兼容只按领域序号续读的调用方也可使用 `after_sequence`；这种方式会跳过该领域事件映射出的全部 AG-UI 子事件。流在 `run.completed`、`run.failed` 或 `run.cancelled` 后结束，长时间无新事件时发送 SSE heartbeat。

映射覆盖 Run 开始/结束/失败、模型文本、工具调用与结果；预算、审批、子 Agent、记忆和上下文事件使用 AG-UI `CUSTOM` 事件保留稳定领域名称。大型工具输出仍使用有界摘要或 Artifact 引用。

## CLI

CLI 直接调用应用服务，不通过本机 HTTP：

```powershell
uv run agentcell run --offline-fake "分析当前项目"
uv run agentcell chat --offline-fake --workspace .
uv run agentcell chat --conversation-id <conversation-id> --offline-fake
uv run agentcell run --model-ref deepseek_pro --max-requests 20 --max-tool-calls 40 --max-input-tokens 300000 --max-total-tokens 360000 "分析当前项目"
uv run agentcell inspect <run-id> --json
uv run agentcell replay <run-id>
uv run agentcell branch <run-id> --from-sequence 18
uv run agentcell resume <run-id> --offline-fake
uv run agentcell resume <run-id> --approval-id <approval-id> --decision approve
uv run agentcell cancel <run-id>
uv run agentcell agents list --offline-fake
uv run agentcell tools list --offline-fake
uv run agentcell memory search "数据库设计" --user-id <uuid> --project-id <project>
```

`agentcell run` 默认限制 10 次模型请求、20 次工具调用、200,000 输入 Token、40,000 输出 Token 和 240,000 总 Token。较大的只读仓库分析可以使用 `--max-requests`（1–100）、`--max-tool-calls`（1–1000）、`--max-input-tokens`（1–2,000,000）和 `--max-total-tokens`（1–2,000,000）按 Run 显式提高额度；这会增加费用和执行时间，不会形成全局永久授权。

运行时会同时检查剩余请求、工具、输入 Token、输出 Token 和总 Token。继续开放工具前必须为下一次探索请求和最终综合请求预留 Token，并保留最多 20% 的请求/工具收尾窗口；达到任一收尾阈值后会隐藏工具，要求模型基于已有证据给出最终答案。CLI 暂不单独覆盖输出 Token，上限使用默认 40,000。

资源命令支持 `--json`。业务错误或数据库错误返回退出码 1，Ctrl+C 返回 130。审批参数修改仍建议通过 HTTP 的结构化 `ApprovalDecision` 提交；CLI 当前提供批准/拒绝交互，避免在 shell 中不安全地拼接任意 JSON。

普通 `run` 输出在最终结果后显示两行运行统计：Run ID、状态、请求数、工具调用数，以及输入/输出/总 Token、缓存读取 Token、缓存写入 Token 和缓存命中率。缓存命中率按 `cache_read_tokens / input_tokens` 计算并限制在 0–100%；Provider 未返回缓存明细时显示 0。`--json` 输出通过 `budget.used.cache_read_tokens` 和 `budget.used.cache_write_tokens` 提供原始累计值。

`agentcell chat` 创建 Conversation，并为每次输入创建独立 Run；启动时会打印 Conversation ID 和用户作用域 ID，退出后可按 Conversation ID 继续。输入 `/exit` 或 `/quit` 安全退出。`agentcell run` 仍保留为无隐式历史继承的单任务入口。

## 阶段 9.2 CLI 当前实现

`run` 与新建 `chat` 现在都接受 `--agent`；coder 默认获得当前工作区读写租约，命令只有通过可重复的 `--allow-command` 显式授予后才可见。继续 Conversation 时 Agent 必须与会话固定作用域一致。CLI 直接消费持久化领域事件并按 sequence 去重，回合完成行同时包含 Run ID 与 Conversation ID，退出或 EOF 会输出续聊命令。

当前可用命令：

```powershell
uv run agentcell run --agent coder --permission-mode request "修复测试失败"
uv run agentcell chat --agent coder --permission-mode request --workspace .
uv run agentcell run --agent coder --allow-write . --allow-command pytest --permission-mode auto "修复并测试"
```

未传 `--model-ref` 时，应用使用 `agentcell.toml` 中声明的第一个模型；当前示例配置中是 `qwen_plus`。真实执行建议显式传入 `--model-ref qwen_plus` 或 `--model-ref deepseek_pro`，避免配置顺序变化影响结果。模型引用是 `[models.<model-ref>]` 的键，不是厂商返回的模型名称。

### Run 能力租约参数

阶段 9.2 当前只有两个 `--allow-*` 参数，均可重复传入：

- `--allow-write <path>`：授予一个工作区相对写作用域，例如 `.`、`src`、`tests`；多个目录要重复传参。路径解析后必须仍在 `--workspace` 内，敏感路径、符号链接/Windows junction 逃逸继续拒绝。coder 未传此参数时默认使用 `.`，其他 Agent 不会因为此参数获得其 AgentSpec 未声明的写工具。
- `--allow-command <name>`：授予一个精确的可执行文件名称，例如 `pytest`、`python`、`uv`、`ruff` 或 `pyright`；多个名称要重复传参。值不能包含路径、空格或参数，实际执行时还必须能从清理后的 PATH 中找到。项目没有隐式“安全命令全集”，本 Run 最终白名单就是用户逐项传入的名称。

没有 `--allow-read`、`--allow-delete`、`--allow-network` 或 `--allow-all`。工作区读取由所选 Agent 和基础租约决定；删除仍受写作用域、DANGEROUS 审批、路径及哈希校验约束；网络尚未通过这组 CLI 参数开放；`--permission-mode full` 也不是 `--allow-all`。

命令按工具参数中的 `command` 字段精确匹配，而不是按最终目的匹配：

```text
command=pytest, args=["tests/"]           -> 需要 --allow-command pytest
command=python, args=["-m", "pytest"]    -> 需要 --allow-command python
command=uv, args=["run", "pytest"]       -> 需要 --allow-command uv
```

因此，只传 `--allow-command pytest` 时，模型改用 `python -m pytest` 会以 `shell_command_denied` 安全失败。若确实接受解释器权限，可同时传 `--allow-command pytest --allow-command python`；`python`、`uv` 等解释器/启动器可执行更广泛代码，权限明显大于单独的 `pytest`，应优先配合 `request` 模式。Shell 从不解析 `2>&1`、管道、重定向等语法，它们只会成为普通参数，不应放入 `args`。

### 阶段 9.2.2 目标 CLI（尚未实现）

阶段 9.2.2 不新增更多 `--allow-*`。后端 AgentSpec、CapabilityLease、Tool scope 和 Approval 分层保持不变，终端入口收敛为：`--approval-mode` 表达审批方式，`--write-scope` 仅在需要时缩小 coder 默认写范围，`--command-profile` 表达受约束的 pytest/ruff/pyright 工作流，`--command` 作为高级精确 executable 入口，`--network-domain` 仅为实际 HTTP Agent 提供域名范围。旧的 `--permission-mode`、`--allow-write`、`--allow-command` 将保留一个版本作为兼容别名。

人类流式输出也将在 9.2.2 收敛：默认 TTY 使用固定高度的“Agent 正在工作”区域，以可读文案滚动/聚合读取、搜索、测试、审批、压缩、文件变化和预算状态；其下方独立流式显示候选回答。Run 完成后动态区自动隐藏，只保留最终回答与运行统计。非 TTY 使用简洁里程碑，`--no-stream`、`--json` 和 `--json-events` 语义保持不变。该区域是公开领域事件的安全执行摘要，不是原始模型思维链。

目标常用形式为：

```powershell
uv run agentcell run --agent coder --model-ref deepseek_pro --approval-mode request --command-profile pytest --workspace G:\Agent-Playground "修复测试"
```

该段描述的是阶段 9.2.2 的实现目标；在完成前仍应使用上文列出的当前参数，CLI 不得假装已经支持新名称。

`run --json-events` 会输出按 Run sequence 排序的稳定 NDJSON 领域事件，并与 `--json` 互斥；该模式不混入 ANSI、提示符或人类摘要。

`--permission-mode request|auto|full` 分别表示“请求审批”“替我审批”“当前工作区完全访问”。审批模式不创建权限：AgentSpec 仍限制工具，`--allow-write`、`--allow-command` 和 CapabilityLease 仍限制路径与命令。`auto` 只能由 PolicyEngine 自动批准租约内 GUARDED 操作；`full` 可自动批准显式租约内 GUARDED/DANGEROUS，但 FORBIDDEN、敏感路径、工作区逃逸、未授权命令和所有既有安全校验永远不能跳过。自动决定会持久化决策主体、理由、Diff、事件和执行账本；因为没有暂停，不创建等待审批检查点。

`request` 模式的审批提示使用以下输入：

- `a`、`approve`、`y`、`yes`：只批准本次调用并继续执行；
- `t`：批准本次调用，并在当前 Run 内临时批准同一工具；
- `m`：输入修改后的工具参数 JSON，再批准；
- `r`、`reject`、`n`、`no`：拒绝；
- `q` 或直接回车：不作决定，Run 保存为 `waiting_approval`。

出现 `status=waiting_approval` 表示操作尚未执行。可用 `agentcell inspect <run-id>` 查看待处理 Approval ID，再通过 `agentcell resume <run-id> --approval-id <approval-id> --decision approve|reject` 处理。阶段 9.2.1 将把恢复时的原始模型身份固定与显式校验列为完成门禁；该门禁完成前，恢复真实 Provider Run 时必须确认应用构建出的 AgentSpec 仍指向原 Run 模型，不能依赖配置顺序静默选择。

TTY 默认使用 Rich 流式渲染模型文本、工具状态、审批、子 Agent、上下文压缩、预算和终态；`--no-stream` 保留批量输出，`--json` 不混入终端样式，可选 `--json-events` 使用 NDJSON。所谓安全推理摘要只显示可公开的进度、证据和决策说明，不得读取或输出 ThinkingPart、Provider reasoning_content 或原始思维链。

每轮结束和退出的目标格式为：

```text
run_id=<run-id> conversation_id=<conversation-id> status=completed ...

Continue with:
uv run agentcell chat --conversation-id <conversation-id>
```

文件变更查询与安全回滚当前可用：

```powershell
uv run agentcell changes list --run <run-id>
uv run agentcell changes show <change-id>
uv run agentcell changes diff <change-id>
uv run agentcell changes revert <change-id>
```

四个命令必须在创建原 Run 时相同的 AgentCell 运行目录中执行，或传入相同的 `--database-url`；默认数据库和 Artifact 位于 `.agentcell/agentcell.db` 与 `.agentcell/artifacts/`。这里使用的是 Run ID 和 Change ID，不是 Conversation ID。

1. `changes list --run <run-id>`：列出一个 Run 的全部持久化文件变化，输出依次为 Change ID、状态、操作和工作区相对路径。状态包括 `prepared/completed/conflict/reverted` 等，操作包括 `created/replaced/patched/deleted/reverted`。没有输出通常表示该 Run 未修改文件或当前连接了另一数据库；加 `--json` 可获得机器可读列表。
2. `changes show <change-id>`：显示一条 FileChange 的 Change/Run 标识、状态、路径和保存的完整正向 Diff；加 `--json` 返回 ChangeSet 与 FileChange 元数据。Change ID 从上一条 `list` 命令取得。
3. `changes diff <change-id>`：只向标准输出打印已持久化的完整 Diff Artifact，适合审阅或重定向保存；它不读取当前 Git Diff，也不调用模型。
4. `changes revert <change-id>`：先打印精确反向 Diff，再要求确认；输入 `y` 才应用，`--yes` 可跳过交互确认。回滚前要求原记录为 `completed`、未被回滚、当前文件哈希仍等于原 after 哈希，并在已记录 Git HEAD 时要求 HEAD 未变化。成功后创建新的反向 FileChange 并保留原审计记录；后续人工修改、重复回滚、Artifact 缺失/篡改或 HEAD 改变都会安全拒绝。它不会执行 Git reset/checkout/restore/clean/stash。

典型流程：

```powershell
uv run agentcell changes list --run 7159d3c1-03aa-4fac-acc7-c5ca7cb7af6b
uv run agentcell changes show 6aa7b35e-5590-4a42-90cf-e45c0dc6e035
uv run agentcell changes diff 6aa7b35e-5590-4a42-90cf-e45c0dc6e035
uv run agentcell changes revert 6aa7b35e-5590-4a42-90cf-e45c0dc6e035
```

对应 HTTP 资源为 `GET /api/runs/{run_id}/changes`、`GET /api/changes/{change_id}`、`GET /api/changes/{change_id}/diff` 和 `POST /api/changes/{change_id}/revert`。查询返回 AgentCell 持久化的 before/after 哈希、Artifact、操作 Diff，以及存在 Git 仓库时的 HEAD、初始 dirty 标记和 path-scoped Git Diff。当前回滚端点要求显式 `confirm=true`，创建新的反向变更并返回稳定 ID；查询路由不写文件。阶段 9.2.1 先把回滚 Lease 改为服务端推导；把回滚进一步纳入独立 Run、Approval/Checkpoint 和预算投影下沉到阶段 11。

Git 是可选增强，不是前提。CLI/API 在非 Git 工作区也必须依靠 ChangeSet/FileChange 与 Artifact 查看和恢复；在 dirty 仓库中只归因本 Run 实际触碰的路径。系统不会自动调用 `git reset --hard`、全工作区 checkout/restore、clean、stash、commit 或 push。
