# `src/agentcell` 目录说明

本目录是 AgentCell 的 Python 主包。这里放置领域模型、运行时编排、基础设施适配器，以及 API/CLI 等输入输出适配层。

当前项目已完成阶段 7.1。审批恢复、记忆/上下文、Artifact 持久化、工作区写入/删除、安全 Shell 和受限 HTTPS 均已有实际实现；telemetry 和 api 仍只有职责边界。

## 目录树

```text
agentcell/
├── README.md
├── __init__.py
├── config.py
├── errors.py
├── agents/
│   ├── __init__.py
│   ├── builtins.py
│   ├── factory.py
│   ├── models.py
│   └── registry.py
├── api/
│   ├── __init__.py
│   └── routes/
│       └── __init__.py
├── budgets/
│   ├── __init__.py
│   ├── models.py
│   └── tracker.py
├── cli/
│   ├── __init__.py
│   └── app.py
├── events/
│   ├── __init__.py
│   └── models.py
├── kernel/
│   ├── __init__.py
│   ├── deps.py
│   ├── checkpoint.py
│   ├── event_recorder.py
│   ├── lifecycle.py
│   ├── model_runtime.py
│   ├── models.py
│   ├── run_service.py
│   ├── replay.py
│   └── tool_bridge.py
├── memory/
│   ├── __init__.py
│   ├── compaction.py
│   ├── injector.py
│   ├── models.py
│   ├── policy.py
│   ├── service.py
│   └── summarizer.py
├── policy/
│   ├── __init__.py
│   ├── approvals.py
│   ├── engine.py
│   └── models.py
├── providers/
│   ├── __init__.py
│   ├── bailian.py
│   ├── base.py
│   ├── deepseek.py
│   ├── factory.py
│   ├── fake.py
│   └── models.py
├── storage/
│   ├── __init__.py
│   ├── artifact_store.py
│   ├── database.py
│   ├── repositories.py
│   └── tables.py
├── telemetry/
│   └── __init__.py
└── tools/
    ├── __init__.py
    ├── artifacts.py
    ├── executor.py
    ├── http.py
    ├── models.py
    ├── registry.py
    ├── shell.py
    └── workspace.py
```

`__pycache__/` 等 Python 自动生成目录不属于源码，不在本文档中列出，也不应提交到 Git。

## 根目录文件

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `README.md` | 已实现 | 说明 `src/agentcell` 下每个源码文件和目录的用途、依赖方向与扩展规则。 |
| `__init__.py` | 已实现 | 定义 `agentcell` Python 包并暴露包版本 `__version__`。不保存 Run、用户、预算或工作区等运行时全局状态。 |
| `config.py` | 已实现 | 使用 pydantic-settings 和 TOML Source 加载严格类型化的模型目录；按稳定 `model_ref` 返回 ModelSpec，不解析或保存明文密钥。 |
| `errors.py` | 已实现 | 项目统一异常层次。当前包含配置、领域、状态转换、事件、预算、存储、Provider、工具、工作区、Shell 和 HTTP 安全错误。跨模块的预期失败应优先使用这里的安全分类异常。 |

## `kernel/`：运行时内核

内核负责 Run 生命周期、运行编排、检查点、恢复、回放和分支。它不能依赖 FastAPI、Typer 或前端协议。

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `kernel/__init__.py` | 已实现 | 暴露当前稳定的 `Run`、`RunStatus`、可用转换查询和转换校验接口。 |
| `kernel/lifecycle.py` | 已实现 | Run 状态机的唯一事实来源。定义 `created`、`running`、`waiting_approval`、`paused`、`completed`、`failed`、`cancelled`，并集中维护合法转换和终态判断。其他模块不得自行修改状态字符串。 |
| `kernel/models.py` | 已实现 | 定义与存储无关的不可变 `Run` 领域模型，统一 UUID、父子关系、UTC 时间，并通过 `transition_to` 调用 lifecycle 校验状态变化。 |
| `kernel/deps.py` | 已实现 | 定义 Run 级依赖对象，显式注入 ID、工作区、租约、预算、事件、工具执行器、Artifact Store、MemoryService 和 Agent Registry。 |
| `kernel/event_recorder.py` | 已实现 | 将一个 Run 的通用事件生产者绑定到事务化 EventStore。 |
| `kernel/model_runtime.py` | 已实现 | 包装 PydanticAI Model，在每次模型请求前执行记忆注入、成对裁剪和 Artifact 压缩，并统一处理预算、Usage、完成事件和分类失败。 |
| `kernel/tool_bridge.py` | 已实现 | 从 ToolRegistry 构造 PydanticAI Tool Schema，同时强制所有实际调用经过 ToolExecutor。 |
| `kernel/run_service.py` | 已实现 | 编排 Run 创建、状态变化、模型/只读工具循环、作用域记忆、Artifact 上下文、文本流事件、硬时长限制，以及完成、失败和取消终态。 |
| `kernel/checkpoint.py` | 已实现 | 定义重启安全的 Checkpoint，保存消息、预算、租约、待审批 ID、临时授权、Artifact UUID 和分支来源。 |
| `kernel/replay.py` | 已实现 | 按连续事件 sequence 回放 Run 状态，并从可恢复检查点创建指定前缀的 paused 子 Run。 |

后续只有出现新的真实职责时才增加内核模块，不能只为匹配规划目录创建空文件。

## `events/`：领域事件

事件层定义可持久化、可回放、可流式映射的业务事实。事件是只追加数据；OpenTelemetry Span 不能替代领域事件。

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `events/__init__.py` | 已实现 | 集中导出事件类型、事件信封、通用 payload 和敏感字段脱敏接口。 |
| `events/models.py` | 已实现 | 定义 24 个核心 `EventType`、泛型 `DomainEvent`、版本化 `EventPayload`、文本增量、错误 payload 和 `ArtifactReference`。负责 UUID、Run 内正整数 sequence、UTC 时间、payload 类型恢复及 API Key、Authorization、密码等字段的递归脱敏。 |

后续事件总线、记录器和具体领域 payload 应保持事件层基础设施中立，不能反向依赖 API、CLI 或 ORM 模型。

## `budgets/`：预算与用量

预算模块负责限制模型请求、Token、工具调用、持续时间、费用、子 Agent 数和委派深度。预算达到上限时必须返回明确错误，不能静默截断。

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `budgets/__init__.py` | 已实现 | 导出预算限制、用量、剩余额度、快照和跟踪器公共接口。 |
| `budgets/models.py` | 已实现 | 定义不可变的 `Budget`、`Usage`、`BudgetRemaining` 和 `BudgetSnapshot` Pydantic 模型。费用使用 `Decimal`，快照时间统一为 UTC。 |
| `budgets/tracker.py` | 已实现 | 实现内存预算记账。Provider 请求、工具和子 Agent 在执行前预留；模型返回后记录实际 Token/费用。真实消耗即使导致超限也会保留。子预算必须适配父 Run 的剩余容量。 |

未来的模型定价表可放入 `pricing.py`，但只有出现实际费用换算职责时再创建。

## `agents/`：Agent 定义与协作

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `agents/__init__.py` | 已实现 | 导出无状态 Agent 声明、Registry、Factory 和当前内置 Agent。 |
| `agents/models.py` | 已实现 | 定义严格、不可变的 AgentSpec，包括模型引用、工具、能力和循环/委派上限。 |
| `agents/registry.py` | 已实现 | 按稳定 ID 注册和查找 AgentSpec，拒绝覆盖并提供确定性排序。 |
| `agents/factory.py` | 已实现 | 使用 ProviderFactory 构造全新的 PydanticAI Agent，不保存 Run 级状态。 |
| `agents/builtins.py` | 已实现 | 提供阶段 5 只读 coordinator 声明。 |

该目录后续负责 coordinator、coder、reviewer、researcher、summarizer 等 Agent 配置。`reviewer` 默认应保持只读。

## `providers/`：模型 Provider 适配

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `providers/__init__.py` | 已实现 | 集中导出稳定 ModelSpec、Usage、输出事件、Factory、Fake 脚本和安全辅助接口。 |
| `providers/models.py` | 已实现 | 定义公共 ModelSpec、百炼/DeepSeek/Fake 专属配置、HTTP 连接池、统一 Usage、模型输出事件和 Fake 故障类型。 |
| `providers/base.py` | 已实现 | 定义最小 ProviderAdapter/SecretResolver 协议、环境密钥解析、HTTPX Client 工厂、完整响应事件映射、错误分类和重试资格判断。 |
| `providers/factory.py` | 已实现 | 按 `model_ref` 和适配器注册表构造 PydanticAI Model；复用可缓存的真实模型，Fake 每次新建；管理自建 HTTP Client 的关闭，不关闭调用方注入的 Client，不执行静默降级。 |
| `providers/bailian.py` | 已实现 | 使用 PydanticAI AlibabaProvider 构造百炼 OpenAI-compatible Model，并映射 `enable_thinking`、`thinking_budget` 和区域端点。 |
| `providers/deepseek.py` | 已实现 | 使用 PydanticAI DeepSeekProvider 构造官方平台 Model，并映射 V4 `thinking.type` 与 `reasoning_effort`。 |
| `providers/fake.py` | 已实现 | 使用 PydanticAI FunctionModel 实现确定性离线脚本，支持文本、固定流式分片、Function Calling、多轮、Usage 和分类故障注入。 |

所有厂商参数映射必须留在对应适配器中。Agent、Tool、API 和 CLI 不得通过 `provider == ...` 判断厂商，也不得记录密钥、完整 Authorization Header、响应体或请求正文。真实 Provider 契约测试默认禁网。

## `tools/`：结构化工具系统

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `tools/__init__.py` | 已实现 | 导出工具协议、Registry、Executor，以及工作区、Shell、HTTP 和 Artifact 公共接口。 |
| `tools/artifacts.py` | 已实现 | 定义与存储实现无关的 ArtifactMetadata，保存受控存储键、媒体类型、字节数、SHA-256 和 UTC 时间。 |
| `tools/models.py` | 已实现 | 定义结构化 ToolCall、大小受限 ToolResult、不可变 ToolDefinition、审批 Preview，以及 Run 绑定的事件、Artifact、账本和执行上下文协议。 |
| `tools/registry.py` | 已实现 | 按稳定名称注册工具，拒绝重复定义、非法名称和非 `extra="forbid"` 参数模型，并集中提供 JSON Schema。 |
| `tools/executor.py` | 已实现 | 统一执行参数校验、能力与审批判断、预算预留、事件顺序、超时、异常分类、JSON 输出校验和 Artifact 转存。未知异常不暴露原始正文，执行器不自动重试。 |
| `tools/workspace.py` | 已实现 | 实现 `workspace.list/read/search/write/patch/delete`，包含读写双作用域、敏感路径、UTF-8/大小、Diff、expected SHA-256、原子写入及 symlink/junction 防护。 |
| `tools/shell.py` | 已实现 | 实现审批式 `shell.run/test`：命令白名单、绝对 PATH 解析、argv 执行、工作区 cwd、最小环境、超时取消和输出上限；不宣称强沙箱。 |
| `tools/http.py` | 已实现 | 实现审批式 `http.request`：HTTPS 443、域名租约、全部 DNS 公网检查、IP 固定、Host/SNI、peer/重定向复核、危险头与敏感 query 拒绝及响应上限。 |

工作区注册函数包含六个文件工具；Shell 和 HTTP 必须分别显式注册。注册本身不授予权限，AgentSpec 和 Run CapabilityLease 仍需同时允许。内置 coordinator 保持只读，避免默认扩大 CLI 权限。

## `policy/`：权限、风险与审批

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `policy/__init__.py` | 已实现 | 导出 RiskLevel、Capability、ToolPolicy、CapabilityLease 和 PolicyEngine。 |
| `policy/models.py` | 已实现 | 定义默认拒绝的文件、网络、命令和委派租约；规范化作用域，拒绝本机/私网/元数据网络范围，并强制子租约只能缩权。 |
| `policy/engine.py` | 已实现 | 在工具执行前统一拒绝 FORBIDDEN、缺少能力和缺少审批的调用。 |
| `policy/approvals.py` | 已实现 | 定义审批影响信封、pending/approved/rejected 状态，以及批准、拒绝、修改参数和当前 Run 临时授权决定。 |

未声明能力默认拒绝。子 Agent 权限必须是父 Agent 权限的子集，委派深度逐层减少；审批已由 RunService 持久化并支持暂停与恢复，新增危险工具仍必须走同一入口。

## `memory/`：记忆与上下文管理

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `memory/__init__.py` | 已实现 | 导出四层记忆领域模型和 Memory Policy；MemoryService 单独导入以避免存储层循环依赖。 |
| `memory/models.py` | 已实现 | 定义 Working、Conversation、Episodic、Semantic 类型，用户/项目/Agent 作用域、候选、持久记忆和排序结果。 |
| `memory/policy.py` | 已实现 | 拒绝凭据特征，要求 Semantic 记忆显式批准，并返回可审查的策略决定。 |
| `memory/service.py` | 已实现 | 提供作用域安全的写入、去重、编辑、删除、过期过滤和综合 FTS5/BM25 排序，并记录写入与召回事件。 |
| `memory/injector.py` | 已实现 | 将有界相关记忆作为“上下文而非指令”的 System Prompt 注入模型消息。 |
| `memory/compaction.py` | 已实现 | 实现 ToolCall/ToolReturn 成对安全裁剪、超大工具结果 Artifact 外置和请求前 ContextManager。 |
| `memory/summarizer.py` | 已实现 | 通过独立 model_ref 生成 Episodic MemoryCandidate，使用低温度并拒绝启用深度思考的配置。 |

首版使用 SQLite FTS5，不以向量数据库为前提。工具调用和工具结果在上下文裁剪时必须成对保留，大内容通过 Artifact 摘要和引用进入上下文。

## `storage/`：SQLite 持久化

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `storage/__init__.py` | 已实现 | 导出数据库、Run/Event/Approval/Checkpoint/Memory/Artifact Repository、工具执行账本和 FileArtifactStore。 |
| `storage/artifact_store.py` | 已实现 | 将有大小上限的内容原子写入受控目录，数据库保存元数据；按内容去重，并在每次加载时校验大小和 SHA-256。 |
| `storage/database.py` | 已实现 | 创建异步 SQLAlchemy Engine/Session，显式提供 session 与事务边界，并为每个连接启用 WAL、外键和 5000ms busy timeout。 |
| `storage/tables.py` | 已实现 | 定义 Run/Event、Approval、Checkpoint、ToolExecution、Memory、Artifact ORM 表和 UTC 时间类型，保持领域模型与 ORM 分离。 |
| `storage/repositories.py` | 已实现 | 提供 Run/Event、审批/检查点、Memory/Artifact Repository 和 SQLite 工具执行账本；包含原子事件 sequence、作用域 FTS5 候选查询及非幂等防重放。 |

Repository 返回领域模型，不向上层泄露 ORM 实例。`run_events` 的唯一约束和只追加触发器由 Alembic 管理；应用代码不得使用 ORM metadata 临时替代生产迁移。

## `telemetry/`：日志与技术追踪

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `telemetry/__init__.py` | 仅职责边界 | 预留结构化日志和 OpenTelemetry 集成。 |

技术追踪用于 Provider 延迟、首 Token、工具耗时、重试和父子 Agent Trace。日志不得包含 API Key、完整环境变量、未脱敏参数或原始模型思维链。

## `api/`：HTTP 与流式适配层

API 层只负责协议适配和依赖注入，不复制 RunService 的业务编排。

| 文件或目录 | 状态 | 职责 |
| --- | --- | --- |
| `api/__init__.py` | 仅职责边界 | 预留 FastAPI 应用工厂、依赖注入、稳定 API Schema 和统一错误映射。 |
| `api/routes/` | 仅职责边界 | 按 runs、agents、memory、system 等稳定资源组织 HTTP 路由。 |
| `api/routes/__init__.py` | 仅职责边界 | 声明 routes Python 子包；当前没有实际路由。 |

SSE/AG-UI 输出必须由领域事件映射而来，并支持按事件 sequence 断线续传。敏感配置不得通过 API 返回。

## `cli/`：命令行适配层

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `cli/__init__.py` | 已实现 | 导出 Typer 应用和控制台脚本入口。 |
| `cli/app.py` | 已实现 | 提供直接调用业务服务的 `run/replay/branch/cancel`，支持显式离线 Fake、JSON 输出、审批提示、迁移提示和 Ctrl+C 语义。 |

CLI 直接调用 RunService/ReplayService，不通过 HTTP 请求本机 FastAPI。当前提供 run、replay、branch、cancel；审批中心交互和 inspect 命令随 API/产品阶段增加。

## 依赖方向

主要依赖方向必须保持为：

```text
api / cli
    ↓
run_service
    ↓
kernel
    ↓
agents / tools / policy / memory / budgets
    ↓
providers / storage / events / telemetry
```

具体约束：

- `kernel` 不依赖 FastAPI、Typer 或 React/AG-UI 协议；
- `providers` 不依赖 API 层；
- `tools` 不直接访问全局数据库会话；
- `storage` 不包含 Agent 决策逻辑；
- `api` 和 `cli` 不复制业务编排；
- 领域模型与 ORM 模型保持分离。

## 新增文件时的维护规则

1. 先确认职责属于哪个现有目录，不创建功能重复的包；
2. 只有出现真实职责时才新增模块，不创建空文件占位；
3. 公共函数必须有类型标注，并使用 `from __future__ import annotations`；
4. 状态机、预算、审批和恢复逻辑变更必须同步添加测试；
5. 新增 Provider、配置、环境变量、迁移、CLI、API、Toolset 或权限模型时同步更新项目文档；
6. 每次新增、删除或调整本目录中的文件职责时，同步更新本 README。
