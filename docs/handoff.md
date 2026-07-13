# AgentCell 项目交接

## 1. 当前状态

更新时间：2026-07-10

项目已经完成“阶段 3：Provider 工程与离线 Fake Provider”，下一步进入“阶段 4：ToolRegistry、能力租约与首批安全工具”。当前具备可测试的领域基础、只追加 SQLite 事件存储、真实模型适配器和离线模型替身，但尚未实现 RunService、可运行 CLI、HTTP API 或 React 应用。

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

## 3. 明确未完成

- 未实现 RunService、工具执行器、审批、检查点或记忆；
- 未实现 CLI 命令、FastAPI 路由、SSE 或 AG-UI 映射；
- 未初始化 Vite/React 依赖和页面；
- 未生成 `pnpm-lock.yaml`；
- 未添加 Dockerfile 或 compose；
- 未实现阶段 4 的 ToolRegistry、CapabilityLease、路径安全与首批只读工作区工具；
- 尚未验证 AG-UI 的具体版本和事件映射。

这些内容被有意留给实际垂直切片，避免生成无法运行或没有测试的空实现。

## 4. 关键决策

### 4.1 骨架只表达稳定边界

当前 `__init__.py` 只说明各包职责，没有提前创建 `runtime.py`、`factory.py`、`database.py` 等空实现文件。后续模块在出现真实职责、测试替身或第二个实现时再创建。

### 4.2 生产依赖按实际切片引入

`pyproject.toml` 已加入阶段 1–3 实际使用的 Pydantic v2、SQLAlchemy 2、Alembic、aiosqlite、PydanticAI slim、pydantic-settings 和 HTTPX。FastAPI 等后续生产依赖仍应在首次使用它们的阶段通过 `uv add` 引入，避免无使用代码却提前固定依赖。

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

EventStore 默认只允许最多 64 KiB 的 UTF-8 JSON payload，并在分配 sequence 前检查。超限会抛出 `EventPayloadTooLargeError`，不会消耗序号。大型内容后续由 Artifact Store 保存，事件使用包含 artifact UUID、媒体类型、字节数和 SHA-256 的 `ArtifactReference`。

### 4.13 ProviderFactory 只构造，不编排

ProviderFactory 根据 `model_ref`、ModelSpec 和适配器注册表返回 PydanticAI Model。它不重试、不更新预算、不写领域事件、不静默切换 Provider；这些职责保留给后续 RunService。自建 HTTPX Client 由 Factory 关闭，注入 Client 由调用方关闭。

### 4.14 密钥按名字解析

配置只保存 `api_key_env` 和可选 `http.proxy_env`，SecretResolver 仅读取明确点名的变量。Provider 分类异常不保留响应体、请求头、完整请求或密钥。真实测试必须同时提供 `AGENTCELL_RUN_LIVE_PROVIDER_TESTS=1` 和对应密钥，只有密钥不会触发线上调用。

### 4.15 Fake Provider 是脚本替身

Fake Provider 基于 PydanticAI FunctionModel，每次构造模型获得独立脚本游标。非流式 Usage 由脚本明确指定，流式 Usage 由固定分片确定性估算。Fake 不伪装厂商，也不回退到真实网络。

## 5. 目录说明

```text
G:\AgentCell
├── docs/
│   ├── development-steps.md
│   ├── handoff.md
│   ├── provider-engineering.md
│   └── technology-stack.md
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
│   ├── policy/
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
│   └── tools/
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
│   └── versions/20260710_0001_create_runs_and_events.py
└── web/src/
    ├── api/
    ├── components/
    ├── hooks/
    ├── pages/
    └── types/
```

## 6. 下一位开发者的建议入口

按以下顺序推进，不要直接从 UI 开始：

1. 阅读 `AGENTS.md`、阶段 4 计划、现有事件、预算和 Provider 模型；
2. 定义 `RiskLevel`、`ToolPolicy`、结构化工具调用和结果模型；
3. 定义 `CapabilityLease`，先锁定父子租约子集规则；
4. 实现 ToolRegistry 和统一执行器，执行前完成 Pydantic 参数及能力检查；
5. 首先交付 `workspace.list/read/search`，实现工作区内路径、符号链接和敏感文件防护；
6. 加入超时、输出字节上限、分类错误以及 `tool.started/completed/failed` 事件边界；
7. 用路径穿越、符号链接逃逸、大文件和输出超限测试锁定安全边界；
8. 写入、删除、Shell 和 HTTP 工具仍留在后续垂直切片，不要绕过审批。

详细验收条件见 `docs/development-steps.md` 的阶段 4。

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
uv run python -m pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Web 尚未初始化，不应运行 `pnpm install` 或假设已有前端脚本。

## 8. 本轮验证结果

在 uv 管理的 Python 3.12.11 环境中执行：

```text
uv run python -m pytest -q    78 passed, 2 skipped（真实 Provider，默认关闭）
uv run ruff check .           passed
uv run ruff format --check .  passed
uv run pyright                0 errors, 0 warnings
uv run alembic upgrade head   passed, 20260710_0001 (head)
uv run alembic check          no new upgrade operations detected
uv lock --check               passed
```

本轮已运行数据库迁移升降级、SQLite PRAGMA、事务回滚、外键、只追加触发器、并发 EventStore 和离线 Provider 契约测试。真实 Provider 与前端测试未运行：前者默认关闭以避免消耗额度，后者尚无应用实现。

## 9. 交接时必须更新的内容

每次阶段性交接都要更新本文件中的：

- 当前状态和更新时间；
- 已完成与明确未完成；
- 新增迁移、配置、环境变量和公开接口；
- 实际执行的测试及结果；
- 已知风险、阻塞项和下一步；
- 任何偏离 `docs/development-steps.md` 的决策及原因。

不得把计划中的模块写成已完成，也不得把未运行的测试写成通过。
