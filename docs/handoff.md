# AgentCell 项目交接

## 1. 当前状态

更新时间：2026-07-13

项目已经完成“阶段 7.1：生产工具扩展”，下一步进入“阶段 8：多 Agent 协作与预算继承”。当前具备带 Diff 和 expected SHA-256 的工作区写入/补丁/删除、argv 白名单 Shell、DNS 固定与逐跳复核的 HTTPS 工具，以及大型 Diff/输出 Artifact 与审批检查点恢复；尚未实现子 Agent、HTTP API 或 React 应用。

## 2. 本轮已完成

- 完整分析 `AGENTS.md`，将架构约束、风险顺序和 M1–M4 拆成可执行步骤；
- 创建 `docs/development-steps.md`；
- 创建 `docs/technology-stack.md`，说明前后端技术职责、边界、接入阶段和官方资料；
- 创建本交接文档；
- 更新根 `README.md`，补充项目定位、文档入口、结构和环境要求；
- 建立 Python 包边界：kernel、agents、providers、tools、policy、memory、budgets、events、storage、telemetry、api、cli；
- 建立测试、Alembic 迁移和 Web 工作区目录；
- 添加基础 `pyproject.toml`、`.gitignore`、`.env.example` 和 `agentcell.toml`；
- 使用 uv 的 Python 3.12.11 同步开发依赖并生成 `uv.lock`；
- 添加包结构烟雾测试；
- 引入实际使用的 Pydantic v2 生产依赖；
- 建立 `AgentCellError`、配置、领域、状态转换和预算错误层次；
- 实现 `RunStatus`、完整状态转换表、终态判断和非法转换错误；
- 定义 24 个核心 `EventType`、泛型 `DomainEvent`、版本化 payload、UTC 规范化和递归凭据脱敏；
- 实现 `Budget`、`Usage`、`BudgetRemaining`、`BudgetSnapshot` 和 `BudgetTracker`；
- 实现模型请求、工具、持续时间、费用、子 Agent 数量、深度及子预算剩余容量检查；
- 对模型调用返回的真实 Token/费用采用“先记账、再报超限”，确保实际消耗不会丢失；
- 添加生命周期、事件、脱敏、预算、子预算、UTC 和 Decimal 序列化单元测试；
- 引入 SQLAlchemy 2、Alembic 和 aiosqlite；
- 实现不可变 Run 领域模型及 UTC 状态转换时间；
- 实现异步 SQLite Engine、Session 和显式事务边界；
- 对每个 SQLite 连接启用 WAL、外键和 5000ms busy timeout；
- 创建 `runs`、`run_events` 首个 Alembic 迁移及升降级路径；
- 实现返回领域模型的 RunRepository；
- 实现只追加 EventStore、payload Schema 恢复和按 sequence 续读；
- 使用 `runs.next_event_sequence` 在数据库事务中原子分配事件序号；
- 使用唯一约束及 SQLite 触发器禁止重复 sequence 和历史事件 UPDATE/DELETE；
- 定义 Artifact 引用契约，并在 EventStore 强制 64 KiB 内联 payload 上限；
- 添加临时数据库、外键、回滚、迁移、并发 sequence 和只追加集成测试。
- 引入 `pydantic-ai-slim[openai]`、pydantic-settings 和 HTTPX，并更新 uv 锁文件；
- 实现严格 ModelSpec、百炼/DeepSeek/Fake 专属配置、HTTP 连接池和 AgentCellSettings TOML 加载；
- 实现统一 ModelUsage、模型文本/工具/完成输出事件及 PydanticAI 响应映射；
- 实现 ProviderAdapter、SecretResolver、环境密钥解析和可注入 HTTPX AsyncClient；
- 实现 ProviderFactory 的可复用真实模型缓存、自建 Client 生命周期和适配器注册表；Fake 模型每次构造使用新脚本游标；
- 使用 PydanticAI AlibabaProvider 接入 `qwen3.7-plus`，映射思考开关、预算和区域端点；
- 使用 PydanticAI DeepSeekProvider 接入 `deepseek-v4-pro`，映射思考开关和 `high`/`max` 推理强度；
- 实现脚本化 Fake Provider，覆盖文本、固定流式分片、Function Calling、多轮工具、Usage 和分类故障；
- 实现认证、权限、模型不存在、限流、超时、连接、上下文、上游和协议错误分类；
- 实现仅允许连接/超时、429、502/503/504 在次数内重试的公共判断；
- 添加默认禁网的 Provider 契约测试和显式付费测试双重开关；
- 新增 `docs/provider-engineering.md` 并同步根 README、技术栈、源码目录和测试文档。
- 实现 `RiskLevel`、五类粗粒度 `Capability` 和严格 `ToolPolicy`；
- 实现默认拒绝的 `CapabilityLease`，规范化文件范围、域名和命令白名单；
- 实现父子租约子集校验，文件和网络只能收窄，命令只能减少，委派深度逐层递减；
- 拒绝网络 scheme、端口、路径、本机、私网、链路本地、保留地址和云元数据地址；
- 实现 PolicyEngine，对 FORBIDDEN、缺少能力和缺少审批统一分类拒绝；
- 实现结构化 ToolCall、ToolResult、ToolDefinition、ToolEventSink、ArtifactStore 和 ToolExecutionContext；
- 实现 ToolRegistry，拒绝覆盖注册、非法名称和非严格参数 Schema；
- 实现统一 ToolExecutor，固定参数、能力、预算、事件、超时、输出和异常处理顺序；
- 成功路径固定发出 `tool.proposed → budget.updated → tool.started → tool.completed`；
- 参数或权限前置失败不消耗工具预算，已启动后的超时和失败保留预算预留；
- 实现 JSON 输出字节上限，超限时必须转存 Artifact，否则明确失败；
- 实现 `workspace.list/read/search` 三个 SAFE、幂等、只读工具；
- 实现盘符/UNC/绝对路径/父目录穿越、敏感路径和租约越界拒绝；
- 实现 symlink 与 Windows reparse point/junction 逃逸防护；
- 实现最大 64 KiB、完整 UTF-8 字符边界的续读，以及有限文件数/文件大小/结果数的字面量搜索；
- 新增 `docs/tool-security.md` 和能力、执行器、临时工作区安全测试。
- 实现无状态 `AgentSpec`、确定性 AgentRegistry、AgentFactory 和只读 coordinator；
- 实现 RunDeps、RunEventRecorder、PydanticAI Tool bridge 与带预算/事件包装的 RunModel；
- 实现 RunService 的创建、运行、模型文本流、单次/多次只读工具调用和原子终态持久化；
- 将 `tool_call_id` 贯穿 Provider Tool Call 与 ToolExecutor 审计事件；
- 实现模型、工具、请求预算、运行时长和用户取消边界，失败信息使用统一脱敏 ErrorPayload；
- 引入 Typer 与 Rich，提供直接调用 RunService 的 `agentcell run`、`--offline-fake` 和 `--json`；
- 添加 Agent、RunService 和 CLI 单元/集成测试，保持阶段 5 严格只读。
- 新增严格 Approval、ApprovalDecision 和 Checkpoint 领域模型；
- 新增 Alembic revision `20260713_0002`，创建 approvals、checkpoints 和 tool_executions；
- 使用 PydanticAI 原生 deferred-tool 输出暂停 Guarded/Dangerous 工具调用；
- 在同一事务中持久化审批、检查点、waiting_approval 投影和领域事件；
- 支持批准、拒绝、修改参数后批准和仅当前 Run 有效的同类临时批准；
- 使用标准 PydanticAI 消息历史和预算快照实现服务重启后恢复；
- 实现重复相同恢复请求幂等、不同决定冲突和取消幂等；
- 实现 ReplayService 的完整/前缀回放和检查点支持的 sequence 分支；
- 实现持久化工具执行账本，阻止 started/failed 非幂等调用重复执行；
- CLI 新增 replay、branch 和 cancel，并在 run 输出中展示审批 ID 与影响；
- 新增 `docs/approval-recovery.md` 和审批、恢复、回放、分支、防重放测试。
- 定义 Working、Conversation、Episodic、Semantic 四层记忆及用户/项目/Agent 作用域；
- 实现 Memory Policy、内容去重、过期、编辑、删除、凭据拒绝和 Semantic 显式批准；
- 使用 SQLite FTS5、BM25、时间衰减、重要度和标签完成作用域排序检索；
- 实现 `MemoryInjector`、`PairSafeTrimmer`、`ToolOutputCompactor` 和独立低温度 `EpisodicSummarizer`；
- 实现文件 Artifact Store、数据库元数据、大小上限、原子写入、内容去重和加载哈希校验；
- 将 Artifact UUID 保存进检查点，并验证进程重启后的恢复加载；
- 新增 Alembic revision `20260713_0003` 及记忆、上下文和 Artifact 集成测试；
- 新增 `docs/memory-context-artifacts.md` 并同步开发、迁移、源码目录和测试文档。
- 实现 `workspace.write/patch/delete`，要求读写双租约、审批 Diff、expected SHA-256、原子写入和非幂等防重放；
- 扩展审批 Preview，支持大型 Diff Artifact，并把引用保存进审批和检查点；
- 对超大工具/审批参数的领域事件改为有界摘要，避免突破 64 KiB 事件上限；
- 实现 `shell.run/test`，使用命令白名单、清洗后的绝对 PATH、argv、工作区 cwd、最小环境、超时和输出限制；
- 实现 `http.request`，使用 HTTPS 443、域名租约、全部 DNS 公网检查、固定 IP、Host/SNI、peer 和重定向逐跳复核；
- 明确所有 Shell 和 HTTP 调用均审批且非幂等，禁止自动重试；
- 新增 `docs/production-tools.md` 和生产工具安全/审批恢复集成测试。

## 3. 明确未完成

- 未实现 FastAPI 路由、SSE 或 AG-UI 映射；
- 未初始化 Vite/React 依赖和页面；
- 未生成 `pnpm-lock.yaml`；
- 未添加 Dockerfile 或 compose；
- 未实现 Memory 或 delegation 工具；
- 未实现子 Agent、预算继承或程序化 Handoff；
- 尚未为生产工具提供完整 CLI/API 审批中心；内置 coordinator 仍默认只读；
- 尚未把 Memory CRUD 暴露为 HTTP/完整 CLI 产品接口；当前由应用服务和运行时使用；
- 尚未验证 AG-UI 的具体版本和事件映射。

这些内容被有意留给实际垂直切片，避免生成无法运行或没有测试的空实现。

## 4. 关键决策

### 4.1 骨架只表达稳定边界

当前 `__init__.py` 只说明各包职责，没有提前创建 `runtime.py`、`factory.py`、`database.py` 等空实现文件。后续模块在出现真实职责、测试替身或第二个实现时再创建。

### 4.2 生产依赖按实际切片引入

`pyproject.toml` 已加入阶段 1–7.1 实际使用的 Pydantic v2、SQLAlchemy 2、Alembic、aiosqlite、PydanticAI slim、pydantic-settings、HTTPX、Typer 和 Rich。阶段 7.1 复用 HTTPX 和标准库进程/网络能力，没有新增生产依赖；FastAPI 等后续依赖仍应在首次使用时通过 `uv add` 引入。

### 4.3 先做离线闭环

推荐的首个可运行目标是：

```text
Run 生命周期 + 预算 + 事件模型 + SQLite EventStore + Fake Provider + 最小 CLI
```

它必须在没有 API Key 和网络的情况下完成确定性测试。

### 4.4 安全与恢复不后补

任何写文件、Shell、网络和子 Agent 能力都要与能力租约、审批、预算、事件、超时和恢复测试一起交付。

### 4.5 状态机是唯一入口

Run 不允许自行写入状态字符串。相同状态的重复设置也不是合法转换；未来 API 的幂等处理应由服务层识别 no-op 或冲突，再调用 lifecycle 校验真实转换。

### 4.6 预算预留与实际记账分离

Provider 请求、工具调用和子 Agent 在执行前预留；模型返回后再记录实际 Token 和费用。预留失败不消耗名额，但上游已经产生的真实 Usage 即使导致超限也必须保留，然后抛出 `BudgetExceededError`。

子预算在启动时必须适配父 Run 完成子 Agent 名额预留后的剩余请求、Token、工具、时间、费用、子 Agent 数和深度容量。阶段 1 只完成边界校验；父子 Run 的实际 Usage 汇总留到多 Agent 阶段。

### 4.7 事件层保持基础设施中立

`events` 不反向依赖 kernel 或 budgets。事件信封使用泛型 payload，公共层只提供通用 payload、UTC/序号约束和安全序列化；具体领域 payload 后续由所属领域模块定义。

### 4.8 事件序号由数据库原子分配

`runs.next_event_sequence` 是每个 Run 的下一个序号。EventStore 使用 `UPDATE ... RETURNING` 在当前事务内递增计数器，再插入事件；若事务回滚，计数器和事件一起回滚。并发连接由 SQLite 写锁、5000ms busy timeout 和 `(run_id, sequence)` 唯一约束共同保护。

### 4.9 历史事件在数据库层只追加

EventStore 不提供更新或删除接口，首个迁移还创建 `trg_run_events_no_update` 和 `trg_run_events_no_delete`。直接 SQL 尝试修改历史事件也会失败。

### 4.10 领域模型与 ORM 分离

`kernel.models.Run` 不依赖 SQLAlchemy。`RunRow` 和 `RunEventRow` 仅存在于 storage，Repository 返回 `Run` 或 `DomainEvent`。RunRepository 不判断状态是否合法，只保存已经通过 `Run.transition_to` 校验的领域投影。

### 4.11 SQLite UTC 表达

UUID 使用 SQLAlchemy `Uuid` 映射；时间使用 storage 的 `UTCDateTime` 保存为 UTC ISO-8601 文本，以避免 SQLite 丢失 timezone 信息。事件 JSON 先经过版本化 Pydantic payload 和敏感字段脱敏，再进入数据库。

### 4.12 大事件内容必须外置

EventStore 默认只允许最多 64 KiB 的 UTF-8 JSON payload，并在分配 sequence 前检查。超限会抛出 `EventPayloadTooLargeError`，不会消耗序号。大型内容由 FileArtifactStore 保存，事件使用包含 artifact UUID、媒体类型、字节数和 SHA-256 的 `ArtifactReference`。

### 4.13 ProviderFactory 只构造，不编排

ProviderFactory 根据 `model_ref`、ModelSpec 和适配器注册表返回 PydanticAI Model。它不更新预算、不写领域事件、不静默切换 Provider；这些职责由 RunModel/RunService 处理。自建 HTTPX Client 由 Factory 关闭，注入 Client 由调用方关闭。

### 4.14 密钥按名字解析

配置只保存 `api_key_env` 和可选 `http.proxy_env`，SecretResolver 仅读取明确点名的变量。Provider 分类异常不保留响应体、请求头、完整请求或密钥。真实测试必须同时提供 `AGENTCELL_RUN_LIVE_PROVIDER_TESTS=1` 和对应密钥，只有密钥不会触发线上调用。

### 4.15 Fake Provider 是脚本替身

Fake Provider 基于 PydanticAI FunctionModel，每次构造模型获得独立脚本游标。非流式 Usage 由脚本明确指定，流式 Usage 由固定分片确定性估算。Fake 不伪装厂商，也不回退到真实网络。

### 4.16 工具只有一个执行入口

ToolExecutor 是参数校验、能力授权、预算预留、事件通知、超时和输出限制的统一入口。Handler 只接收已经验证的参数和显式 Run 依赖，不直接访问全局数据库。执行器不自动重试，即使工具声明幂等；后续重试必须由 RunService 在事件和预算控制下实现。

### 4.17 路径按解析后结果授权

WorkspacePathResolver 先拒绝明显的绝对路径、盘符、UNC、`..` 和敏感名称，再解析真实路径，并同时检查工作区边界和租约边界。搜索不跟随 link/reparse point；直接读取链接时最终目标必须仍在获准范围内。该实现是进程级防护，不等同于敌意并发写入环境下的强沙箱。

### 4.18 大工具输出不能只截断丢弃

工具输出按 UTF-8 JSON 实际字节数限制。超限且存在 ArtifactStore 时保存完整内容并返回 ArtifactReference；没有 Store 时抛出 `ToolOutputTooLargeError`。阶段 7 的 RunService 已注入实际文件 Store，并在加载时复核大小和 SHA-256。

### 4.19 RunService 复用 PydanticAI 循环

AgentCell 不重写 Function Calling 协议。RunModel 在 PydanticAI Model 边界统一预留请求、记录 Usage 和模型事件；Tool bridge 只暴露 Registry 中 Agent 获准的 Schema，并把所有调用送入唯一 ToolExecutor。RunService 负责 Run 生命周期、硬时长边界和最终事件，不包含厂商判断。

### 4.20 CLI 不自动迁移数据库

`agentcell run` 直接调用 RunService，不请求本机 HTTP，也不会在应用启动时临时创建或修改表。使用者必须先运行 `uv run alembic upgrade head`。Fake Provider 只有显式指定 `--offline-fake` 才启用，不作为真实 Provider 的静默降级。

### 4.21 恢复使用框架原生 deferred-tool

工具桥把 AgentCell 的审批要求转换为 PydanticAI ApprovalRequired，RunService 接收 DeferredToolRequests 后保存标准消息历史。批准、修改和拒绝分别通过 ToolApproved、override_args 和 ToolDenied 恢复，避免实现第二套 Function Calling 协议。

### 4.22 非幂等调用先记账再执行

每个带 Provider Call ID 的工具调用在实际 Handler 前写入 tool_executions。completed 结果可安全复用；started/failed 的非幂等调用拒绝恢复重放。该账本不能判断外部副作用是否已发生，因此对不确定状态采取默认拒绝。

## 5. 目录说明

```text
G:\AgentCell
├── docs/
│   ├── development-steps.md
│   ├── handoff.md
│   ├── provider-engineering.md
│   ├── technology-stack.md
│   └── tool-security.md
├── src/agentcell/
│   ├── config.py
│   ├── errors.py
│   ├── agents/
│   ├── api/routes/
│   ├── budgets/models.py
│   ├── budgets/tracker.py
│   ├── cli/
│   ├── events/models.py
│   ├── kernel/lifecycle.py
│   ├── kernel/models.py
│   ├── memory/
│   ├── policy/engine.py
│   ├── policy/models.py
│   ├── providers/base.py
│   ├── providers/bailian.py
│   ├── providers/deepseek.py
│   ├── providers/factory.py
│   ├── providers/fake.py
│   ├── providers/models.py
│   ├── storage/database.py
│   ├── storage/repositories.py
│   ├── storage/tables.py
│   ├── telemetry/
│   ├── tools/executor.py
│   ├── tools/models.py
│   ├── tools/registry.py
│   └── tools/workspace.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── provider_contract/
│   ├── replay/
│   └── e2e/
├── alembic.ini
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       ├── 20260710_0001_create_runs_and_events.py
│       ├── 20260713_0002_add_approvals_checkpoints.py
│       └── 20260713_0003_add_memory_artifacts.py
└── web/src/
    ├── api/
    ├── components/
    ├── hooks/
    ├── pages/
    └── types/
```

## 6. 下一位开发者的建议入口

按以下顺序推进阶段 8，并保持子 Agent 权限和预算只能收窄：

1. 阅读 `AGENTS.md`、阶段 8 计划和现有 AgentSpec、CapabilityLease、BudgetTracker、Checkpoint；
2. 定义 coordinator、coder、reviewer、researcher、summarizer 的实际工具与能力集合，reviewer 默认只读；
3. 实现父 Run 分配子预算，请求数、Token、费用、工具调用、持续时间、数量和深度均不得超过父剩余量；
4. 实现 Agent as Tool 的结构化委派、状态事件和父子 Run 关联；
5. 实现 Coordinator → Coder → Reviewer → Finalizer 的程序化 Handoff；
6. 将父子关系、预算和未完成委派写入检查点并支持恢复；
7. 覆盖越权租约、预算超分、最大深度、reviewer 写入拒绝和子 Agent 失败传播测试。

详细验收条件见 `docs/development-steps.md` 的阶段 8。

## 7. 环境与命令

当前检查到的本机工具版本：

```text
PowerShell 默认 Python 3.10.9
uv 项目环境 Python 3.12.11
uv 0.7.13
Node.js 22.17.0
pnpm 9.15.9
```

默认 Python 不满足项目的 Python 3.12+ 要求，但 uv 已创建合格的项目环境。后续应始终通过 uv 执行，例如：

```powershell
uv sync --python 3.12 --dev
uv run alembic upgrade head
uv run agentcell run --offline-fake "分析当前项目"
uv run agentcell run --offline-fake --json "分析当前项目"
uv run agentcell replay <run-id>
uv run agentcell branch <run-id> --from-sequence 18
uv run agentcell cancel <run-id>
uv run python -m pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Web 尚未初始化，不应运行 `pnpm install` 或假设已有前端脚本。

## 8. 本轮验证结果

在 uv 管理的 Python 3.12.11 环境中执行：

```text
uv run python -m pytest -q    158 passed, 2 skipped（真实 Provider，当前进程未启用付费开关）
uv run ruff check .           passed
uv run ruff format --check .  passed
uv run pyright                0 errors, 0 warnings
uv run alembic upgrade head   passed, 20260713_0003 (head)
uv run alembic check          no new upgrade operations detected
uv lock --check               passed
```

本轮已运行数据库迁移、审批恢复、工作区创建/补丁/删除、哈希冲突、Diff Artifact 检查点、junction 逃逸、Shell argv/环境/输出边界、HTTP DNS 固定/重定向/SSRF/响应上限和非幂等防重放测试。默认数据库保持 `20260713_0003 (head)`，阶段 7.1 无 Schema 变更；Alembic check 无漂移。当前测试进程未启用付费 Provider 开关，因此两项真实 Provider 测试按设计跳过且没有产生线上调用。前端测试未运行，因为尚无应用实现。

## 9. 交接时必须更新的内容

每次阶段性交接都要更新本文件中的：

- 当前状态和更新时间；
- 已完成与明确未完成；
- 新增迁移、配置、环境变量和公开接口；
- 实际执行的测试及结果；
- 已知风险、阻塞项和下一步；
- 任何偏离 `docs/development-steps.md` 的决策及原因。

不得把计划中的模块写成已完成，也不得把未运行的测试写成通过。
