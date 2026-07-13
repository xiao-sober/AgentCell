# `src/agentcell` 目录说明

本目录是 AgentCell 的 Python 主包。这里放置领域模型、运行时编排、基础设施适配器，以及 API/CLI 等输入输出适配层。

当前项目已完成阶段 3。`kernel/`、`events/models.py`、`budgets/`、`storage/`、`providers/`、`config.py` 和 `errors.py` 已有实际实现；其他只有 `__init__.py` 的目录目前用于声明稳定职责边界，尚不代表对应功能已经完成。

## 目录树

```text
agentcell/
├── README.md
├── __init__.py
├── config.py
├── errors.py
├── agents/
│   └── __init__.py
├── api/
│   ├── __init__.py
│   └── routes/
│       └── __init__.py
├── budgets/
│   ├── __init__.py
│   ├── models.py
│   └── tracker.py
├── cli/
│   └── __init__.py
├── events/
│   ├── __init__.py
│   └── models.py
├── kernel/
│   ├── __init__.py
│   ├── lifecycle.py
│   └── models.py
├── memory/
│   └── __init__.py
├── policy/
│   └── __init__.py
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
│   ├── database.py
│   ├── repositories.py
│   └── tables.py
├── telemetry/
│   └── __init__.py
└── tools/
    └── __init__.py
```

`__pycache__/` 等 Python 自动生成目录不属于源码，不在本文档中列出，也不应提交到 Git。

## 根目录文件

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `README.md` | 已实现 | 说明 `src/agentcell` 下每个源码文件和目录的用途、依赖方向与扩展规则。 |
| `__init__.py` | 已实现 | 定义 `agentcell` Python 包并暴露包版本 `__version__`。不保存 Run、用户、预算或工作区等运行时全局状态。 |
| `config.py` | 已实现 | 使用 pydantic-settings 和 TOML Source 加载严格类型化的模型目录；按稳定 `model_ref` 返回 ModelSpec，不解析或保存明文密钥。 |
| `errors.py` | 已实现 | 项目统一异常层次。当前包含配置、领域、状态转换、事件 payload、预算、分类存储错误，以及不保留响应体或密钥的 Provider 错误。跨模块的预期业务失败应优先使用这里的异常。 |

## `kernel/`：运行时内核

内核负责 Run 生命周期、运行编排、检查点、恢复、回放和分支。它不能依赖 FastAPI、Typer 或前端协议。

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `kernel/__init__.py` | 已实现 | 暴露当前稳定的 `Run`、`RunStatus`、可用转换查询和转换校验接口。 |
| `kernel/lifecycle.py` | 已实现 | Run 状态机的唯一事实来源。定义 `created`、`running`、`waiting_approval`、`paused`、`completed`、`failed`、`cancelled`，并集中维护合法转换和终态判断。其他模块不得自行修改状态字符串。 |
| `kernel/models.py` | 已实现 | 定义与存储无关的不可变 `Run` 领域模型，统一 UUID、父子关系、UTC 时间，并通过 `transition_to` 调用 lifecycle 校验状态变化。 |

后续出现真实职责时，可在此目录增加 `runtime.py`、`run_service.py`、`checkpoint.py` 和 `replay.py`，但不能只为匹配规划目录创建空文件。

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
| `agents/__init__.py` | 仅职责边界 | 预留无状态 `AgentSpec`、Agent Registry、Factory、内置 Agent 和委派接口。不得在全局 Agent 单例中保存 Run ID、会话、预算或工作区。 |

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
| `tools/__init__.py` | 仅职责边界 | 预留 Tool 模型、Registry、统一执行器，以及 workspace、shell、HTTP、memory、artifact 和 delegation 工具。 |

每个工具必须具有结构化参数、`ToolPolicy`、能力租约检查、超时、输出限制、事件和幂等性声明。Shell 默认关闭，路径必须限制在工作区内。

## `policy/`：权限、风险与审批

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `policy/__init__.py` | 仅职责边界 | 预留风险等级、`CapabilityLease`、Policy Engine 和审批决策。 |

未声明能力默认拒绝。子 Agent 权限必须是父 Agent 权限的子集；写入、删除、Shell 和非只读网络操作按风险要求审批或拒绝。

## `memory/`：记忆与上下文管理

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `memory/__init__.py` | 仅职责边界 | 预留 Working、Conversation、Episodic、Semantic 四层记忆，以及检索、压缩、摘要和 Memory Policy。 |

首版使用 SQLite FTS5，不以向量数据库为前提。工具调用和工具结果在上下文裁剪时必须成对保留，大内容通过 Artifact 摘要和引用进入上下文。

## `storage/`：SQLite 持久化

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `storage/__init__.py` | 已实现 | 导出 `Database`、`RunRepository`、`EventStore` 和 SQLite URL 构造接口。 |
| `storage/database.py` | 已实现 | 创建异步 SQLAlchemy Engine/Session，显式提供 session 与事务边界，并为每个连接启用 WAL、外键和 5000ms busy timeout。 |
| `storage/tables.py` | 已实现 | 定义与领域模型分离的 `RunRow`、`RunEventRow`、ORM metadata，以及将时区时间稳定保存为 UTC ISO-8601 文本的类型。 |
| `storage/repositories.py` | 已实现 | `RunRepository` 在领域模型与 ORM 行之间转换；`EventStore` 在同一事务内原子递增 Run sequence 并追加事件，支持按 sequence 查询、payload Schema 恢复和 64 KiB 内联大小限制。超大内容必须改用 Artifact 引用。 |

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
| `cli/__init__.py` | 仅职责边界 | 预留 Typer 命令和 Rich 流式输出。 |

CLI 必须直接调用 RunService，不能通过 HTTP 请求本机 FastAPI。后续应支持交互审批、JSON 输出、Ctrl+C 取消和有意义的退出码。

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
