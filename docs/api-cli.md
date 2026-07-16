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
uv run agentcell resume <run-id> --approval-id <approval-id> --decision modify --arguments-json '{"path":"src/example.py"}'
uv run agentcell resume <run-id> --approval-id <approval-id> --decision approve --grant-same-tool
uv run agentcell cancel <run-id>
uv run agentcell agents list --offline-fake
uv run agentcell tools list --offline-fake
uv run agentcell memory search "数据库设计" --user-id <uuid> --project-id <project>
```

`agentcell run` 默认限制 10 次模型请求、20 次工具调用、200,000 输入 Token、40,000 输出 Token 和 240,000 总 Token。较大的只读仓库分析可以使用 `--max-requests`（1–100）、`--max-tool-calls`（1–1000）、`--max-input-tokens`（1–2,000,000）和 `--max-total-tokens`（1–2,000,000）按 Run 显式提高额度；这会增加费用和执行时间，不会形成全局永久授权。

运行时会同时检查剩余请求、工具、输入 Token、输出 Token 和总 Token。继续开放工具前必须为下一次探索请求和最终综合请求预留 Token；请求收尾窗口至少覆盖一次初始最终输出和两次校验重试，小阶段预算会在仅剩三次请求时隐藏工具。工具窗口仍使用有界比例并在额度耗尽时立即隐藏工具。CLI 暂不单独覆盖输出 Token，上限使用默认 40,000。

资源命令支持 `--json`。业务错误或数据库错误返回退出码 1，Ctrl+C 返回 130。CLI 的交互审批支持批准、拒绝、修改参数、同工具临时授权和保持 pending；非交互恢复必须显式提供 `--decision`，修改参数还必须提供 `--arguments-json`，不得仅给出 Approval ID 后默认批准。

普通 `run` 输出在最终结果后显示两行运行统计：Run ID、状态、请求数、工具调用数，以及输入/输出/总 Token、缓存读取 Token、缓存写入 Token 和缓存命中率。缓存命中率按 `cache_read_tokens / input_tokens` 计算并限制在 0–100%；Provider 未返回缓存明细时显示 0。`--json` 输出通过 `budget.used.cache_read_tokens` 和 `budget.used.cache_write_tokens` 提供原始累计值。

`agentcell chat` 创建 Conversation，并为每次输入创建独立 Run；启动时会打印 Conversation ID 和用户作用域 ID，退出后可按 Conversation ID 继续。输入 `/exit` 或 `/quit` 安全退出。auto Conversation 会把问候、身份/能力询问和短一般知识问题直接交给无工具 Assistant，以避免额外路由模型调用和 child；涉及项目、文件、测试、修改、审查或研究的任务仍进入 Task Router。`agentcell run` 仍保留为无隐式历史继承的单任务入口。

## 阶段 9.2.2 CLI 当前实现

`run` 与新建 `chat` 共用唯一 `CliRunProfile`。`--agent` 选择 AgentSpec 上限，profile 只在该上限内生成最小 CapabilityLease；继续 Conversation 时 Agent 和 `model_ref` 都必须与固定作用域一致。coder 默认获得当前工作区读写但不获得任何命令，普通 coordinator、reviewer 和 researcher 保持只读，coordinator 的普通 CLI/API Run 为 `max_children=0`。Resume 不重建或覆盖 profile，而是使用 Checkpoint 中原始 Agent、模型、Lease、预算和审批模式。

当前可用命令：

```powershell
uv run agentcell run --agent coder --approval-mode request "修复测试失败"
uv run agentcell chat --agent coder --approval-mode request --workspace .
uv run agentcell run --agent coder --write-scope src --command-profile pytest --approval-mode auto "修复并测试"
uv run agentcell run --agent researcher --network-domain docs.example.com "检索工作区和批准域名的证据"
```

独立 Run 或新 Conversation 未传 `--model-ref` 时，应用使用 `agentcell.toml` 中声明的第一个模型；当前示例配置中是 `qwen_plus`。创建 Conversation 时会把选择结果持久化为固定 `model_ref`，以后只传 `--conversation-id` 也会继续使用原模型，例如原 DeepSeek 会话不会因新进程默认值变成 Qwen。显式提供不同 `--model-ref` 会以 `conversation_model_binding` 冲突失败；CLI 启动行会显示实际绑定。revision `20260715_0009` 会从既有已完成 Run 的执行身份回填历史会话；无法推断的空历史旧会话要求显式绑定一次。

模型引用是 `[models.<model-ref>]` 的键，不是厂商返回的模型名称。每个 Run 仍会持久化完整 AgentSpec/ModelSpec 快照与哈希；v2 身份对 capability 集合使用确定性排序，历史 v1 身份按集合语义兼容。审批恢复、委派和分支不得因配置顺序、默认模型或持久化 Agent 覆盖而静默切换；同一 `model_ref` 的实际 ModelSpec 定义发生变化时仍以 `run_identity_mismatch` 安全失败。

### Run 能力与审批参数

- `--approval-mode request|auto|full`：只决定有效租约内由人工还是确定性 PolicyEngine 审批，不创建工具、路径、命令或网络权限；
- `--write-scope <path>`：缩小工作区相对写作用域，可重复。coder 未传时默认 `.`；不具备写 capability/工具的 Agent 使用该参数会明确失败；
- `--command-profile pytest|ruff|pyright`：只把同名 exact executable 加入命令租约，不隐式授予 `python`、`uv`、Shell 字符串或其他启动器；
- `--command <name>`：高级精确 executable 入口，可重复；值不能包含路径、空格或参数；
- `--network-domain <domain>`：只对具备 network capability 和 HTTP 工具的 Agent 生效，可重复，不接受 scheme、path、port、本机或元数据地址。

旧 `--permission-mode`、`--allow-write`、`--allow-command` 隐藏保留一个版本。它们生成与新参数相同的 Lease，并只在人类输出中提示弃用；JSON/NDJSON 不混入提示。新旧同类参数同时出现会失败，避免不明确的覆盖顺序。没有 `--allow-read`、`--allow-delete`、`--allow-network` 或 `--allow-all`。

命令按工具参数中的 `command` 字段精确匹配，而不是按最终目的匹配：

```text
command=pytest, args=["tests/"]           -> --command-profile pytest 或 --command pytest
command=python, args=["-m", "pytest"]    -> 必须显式 --command python
command=uv, args=["run", "pytest"]       -> 必须显式 --command uv
```

因此，只传 `--command-profile pytest` 时，模型改用 `python -m pytest` 或 `uv run pytest` 会安全失败。`python`、`uv` 等解释器/启动器可执行更广泛代码，只有用户明确接受时才使用 `--command` 单独授予。Shell 从不解析 `2>&1`、管道、重定向等语法。

### 可读工作动态与安全展示

默认 TTY 使用单一、固定上限的 Rich Live“Agent 正在工作”区和回答区：相同工具调用的 proposed/started/completed 更新同一活动，连续 read/search 聚合计数并保留最近安全路径；预算只更新底部摘要。出现审批时先停止 Live，再显示完整审批信封并读取输入；完成后 transient 动态区消失，只保留一次最终答案和 Run 统计。非 TTY 只输出去重里程碑，`--no-stream` 保留批量答案，JSON/NDJSON 不输出 ANSI、人类里程碑或弃用提示。

展示状态由 transport-neutral `RunDisplayProjector` 从有序、脱敏 DomainEvent 确定性重建。模型文本先进入 `answer_candidate`；若随后调用工具则折叠为阶段活动，只有 `run.completed` 才晋升为 `answer`。`run.failed`、`model.output_rejected` 和 pending 不会把伪工具协议晋升为最终答案。ToolDisplayCatalog 只允许 path/query/command/cwd/去 query URL 等字段进入摘要，ThinkingPart、`reasoning_content`、inline 凭据、完整参数和未脱敏工具输出不得进入 RunDisplayState 或 Live。

常用形式为：

```powershell
uv run agentcell run --agent coder --model-ref deepseek_pro --approval-mode request --command-profile pytest --workspace G:\Agent-Playground "修复测试"
```

### 阶段 9.3 显式 software Team（已实现）

```powershell
uv run agentcell run --workspace G:\Agent-Playground --team software --model-ref deepseek_pro --approval-mode request --command-profile pytest "修复测试并独立审查"
```

`--team` 与显式 `--agent` 互斥。`software@v1` 固定运行 Coordinator→Coder→Reviewer→Finalizer，每阶段创建真实子 Run；模型执行身份、子 Budget、子 CapabilityLease、阶段职责、父子委派和检查点均持久化。Team 复用 `--write-scope`、`--command-profile`/`--command`、`--approval-mode` 和预算覆盖参数：profile 先建立 root 上限，再生成不能扩权的子租约和不能超额的子预算。Team 默认 root 预算为 24 次模型请求和 48 次工具调用；默认请求分区为 6/9/6/3，工具硬上限为 3/27/6/0，未分配工具额度留在 root 而不是鼓励阶段耗尽。每阶段最小请求数由允许的探索轮数加三次最终输出机会构成，root 的显式 `--max-requests` 低于 18 时会在启动前报错。Coordinator 因此最多执行三个工作区工具调用，同时有足够请求生成最终计划；Coder 仍默认可写当前工作区但没有命令，测试命令必须显式授权。

Provider 可以在一次模型响应中同时提出多个工具调用。若该批次大于剩余实际工具额度，AgentCell 只执行额度内的前缀；超额尾部调用记录 `tool.failed / budget_exceeded`，以结构化“未执行”结果返回模型，并立即进入无工具最终回答。该处理不会扩大工具预算，也不会因为同一响应的尾部调用把整个 Team 提前打成失败。

Coder 优先运行显式授权的完整测试。运行时使用保守、确定性的测试修复意图识别；命中后，只有 `shell.test` 返回结构化的真实执行成功证据才会跨审批检查点保存并立即关闭该 Coder 的后续工具，只允许报告无需修复。`pytest --collect-only`、无法识别的输出或单纯退出码 0 都不会启用快路径。包含实现新功能、添加模块或重构等增量意图的任务也不会启用该快路径。测试失败时才围绕持久化失败证据读取相关文件并实施最小修改。

Reviewer 子租约固定只读，输出首个非空行必须是 `PASS` 或 `CHANGES_NEEDED`。Handoff 从持久化 `tool.completed` 和 ChangeSet/FileChange 账本生成有界证据，包含测试命令、退出码、结果摘要、Artifact 引用和变更数量；测试成功且文件变更为零时，Reviewer 子租约进一步收窄为空，只依据该证据判断。只有 `PASS` 才启动 Finalizer；Finalizer 的 AgentSpec、子租约和工具预算均不提供工具。子审批会暂停 root Team Run，TTY 可原地处理；非交互环境会输出 Approval ID，之后使用 `agentcell resume <root-run-id> --approval-id <id> --decision approve` 继续。人类终态输出 root Run ID、所有已创建 child Run ID 和 Conversation ID；失败时额外输出 `stage`、`code` 和 `message`。

审批恢复对工具可见性的例外只覆盖待执行的 deferred 工具起点；后续模型请求重新应用请求、Token 和工具余量的最终回答窗口。若批准后子 Run 仍因 Provider、预算或工具错误失败，HandoffService 会结算增量 Usage、把完整 Usage 写回 `agent_delegations.accounted_usage`、记录 child completion，并把 root 从 `paused` 收敛到 `failed`，CLI 返回 Team 的结构化失败而不是模糊的 `Run failed ... status=paused`。

若模型把目录交给 `workspace.read` 或把文件交给 `workspace.list`，错误会在无副作用 preflight 阶段记录为 `workspace_path_type_error`，并允许一次计入模型请求预算的参数纠正；原误调用不消耗工具调用预算。第二次仍出错时 Team 才以结构化失败终止。该机制不适用于路径逃逸、敏感路径或越权请求。

### 阶段 9.4 统一任务入口（已实现）

当前已实现版本化路由 DTO、确定性规则、public Registry/Provider/Lease/Budget 校验、任务 root 和 `task.route_proposed/confirmed/overridden/rejected` 事件。`TaskRoutingService.preview` 不创建 Run、不执行工具；权威 `prepare` 先创建 `task-router` root，再记录不含任务原文和模型思维链的路由事件。能力差额进入 `paused`，硬校验失败进入带结构化原因的 `failed`。9.4.3 的 `confirm/reject/execute/resume/decide_approval` 使用 decision hash 和 `TASK_ROUTE` 检查点恢复；single Agent 创建一个直接 child，software Team 在同一 root 下运行四个 stage child，预算与终态回收到 root。

CLI `run` 和工作区型 chat 回合默认进入该执行入口；`--agent`、`--team` 保留为显式覆盖。普通 chat 快速路径不改变 `POST /api/tasks` 的任务语义。`run --dry-route` 和 `POST /api/task-routes` 不创建 Run 或执行工具；`POST /api/tasks` 创建权威 root，待确认决定通过 `/api/tasks/{run_id}/confirm|reject` 处理。歧义任务调用有界 PydanticAI 结构化分类，并把权威调用 Usage 结算到 root；失败时降级为需要确认的只读 Coordinator。single-Agent child 的公开文本增量，以及 single-Agent/Team child 的工具生命周期和当前 Budget Usage 会按 child sequence 投影到 root。Root 的限制保持不变，显示 Usage 只叠加尚未结算的 child 增量，暂停恢复时以 `accounted_usage` 去重。Rich CLI 显示 Agent、工具名、白名单路径/查询词/命令摘要并聚合连续读取；工作中回答只显示适应终端高度的最新尾窗，完成后输出全文。投影不会复制工具输出、Artifact 内容或完整 Diff。

阶段 9.4 后，普通任务提交不要求用户先选择 Agent 或 Team：

```powershell
uv run agentcell run "检查项目并修复测试问题"
uv run agentcell chat
```

模型、工作区、Agent/Team、权限和预算使用配置默认值，需要时仍可显式覆盖。CLI 直接调用应用层 `TaskRoutingService`，Web 通过同一服务的 API DTO 获取结果。路由候选限定为 public 单 Agent 和确定性 Team；确定性规则优先，歧义时才使用有预算和超时的结构化模型回退。输出展示执行模式、Agent/Team、简短依据、置信度、预计能力和预算档位，不展示原始路由思维链。

`--agent`、`--team`、模型、能力范围和预算参数仍是高级覆盖。覆盖不能突破 AgentSpec、CapabilityLease、Policy 或 Budget，并写入 `task.route_overridden`。低置信度、需要新增写入/Shell/网络能力或 fixed Conversation 执行身份变化时必须确认。auto Conversation 可在版本化 RoutingPolicy 允许的 public Agent/Team 中逐回合路由，但不得继承上一轮临时审批、Lease 或预算。

`--dry-route` 和 `POST /api/task-routes` 不创建 Run、不执行工具，只返回非权威预览；若歧义需要模型回退，仍可能消耗明确上限内的 Token/费用。`POST /api/tasks` 先创建 root Run，再把路由模型 Usage、决定和覆盖写入该 Run 后启动执行。API 路由层不实现关键词分类，CLI 也不通过本机 HTTP 调用服务。`inspect/resume/changes/agents/serve` 等管理命令不进入 Task Router。

`run --json-events` 会输出按 Run sequence 排序的稳定 NDJSON 领域事件，并与 `--json` 互斥；该模式不混入 ANSI、提示符或人类摘要。

`--approval-mode request|auto|full` 分别表示“请求审批”“替我审批”“当前工作区完全访问”。审批模式不创建权限：AgentSpec 仍限制工具，`--write-scope`、`--command-profile`/`--command`、`--network-domain` 和 CapabilityLease 仍限制路径、命令与网络。`auto` 只能由 PolicyEngine 自动批准租约内 GUARDED 操作；`full` 可自动批准显式租约内 GUARDED/DANGEROUS，但 FORBIDDEN、敏感路径、工作区逃逸、未授权命令和所有既有安全校验永远不能跳过。自动决定会持久化决策主体、理由、Diff、事件和执行账本；因为没有暂停，不创建等待审批检查点。

`request` 模式的审批提示使用以下输入：

- `a`、`approve`、`y`、`yes`：只批准本次调用并继续执行；
- `t`：批准本次调用，并在当前 Run 内临时批准同一工具；
- `m`：输入修改后的工具参数 JSON，再批准；
- `r`、`reject`、`n`、`no`：拒绝；
- `q` 或直接回车：不作决定，Run 保存为 `waiting_approval`。

出现 `status=waiting_approval` 表示操作尚未执行。可用 `agentcell inspect <run-id>` 查看待处理 Approval ID，再通过 `agentcell resume <run-id> --approval-id <approval-id> --decision approve|reject|modify` 处理；`modify` 必须同时提供 `--arguments-json`，`--grant-same-tool` 只在批准类决定中生效。恢复会先校验不可变执行身份并重新执行无副作用 preflight；文件内容或 Diff 已变化、权限不再满足、Approval 不属于目标 Run 时均保持安全失败，不会先写入 `tool.approved`。

TTY 默认使用 Rich Live 投影模型候选文本、工具状态、审批、上下文压缩、预算和终态；`--no-stream` 保留批量输出，`--json` 不混入终端样式，可选 `--json-events` 使用 NDJSON。该区域只显示可公开进度、证据和决策说明，不读取或输出 ThinkingPart、Provider reasoning_content 或原始思维链。

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

对应 HTTP 资源为 `GET /api/runs/{run_id}/changes`、`GET /api/changes/{change_id}`、`GET /api/changes/{change_id}/diff` 和 `POST /api/changes/{change_id}/revert`。查询返回 AgentCell 持久化的 before/after 哈希、Artifact、操作 Diff，以及存在 Git 仓库时的 HEAD、初始 dirty 标记和 path-scoped Git Diff。回滚端点只接受显式 `confirm=true`；客户端提交 `lease` 或其他额外字段会返回 422。服务端从 ChangeSet 工作区与原路径推导最小租约，创建新的反向变更并返回稳定 ID；把回滚进一步纳入独立 Run、Approval/Checkpoint 和预算投影下沉到阶段 11。

Git 是可选增强，不是前提。CLI/API 在非 Git 工作区也必须依靠 ChangeSet/FileChange 与 Artifact 查看和恢复；在 dirty 仓库中只归因本 Run 实际触碰的路径。系统不会自动调用 `git reset --hard`、全工作区 checkout/restore、clean、stash、commit 或 push。
