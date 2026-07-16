# AGENTS.md

> 本文件是 AgentCell 仓库中所有编码 Agent、自动化开发工具和贡献者的最高优先级工程说明。  
> 在修改任何代码前，必须先阅读本文件。若子目录存在更具体的 `AGENTS.md`，则子目录文件在其作用域内优先。

---

## 1. 项目定义

### 1.1 项目名称

**AgentCell**

### 1.2 一句话定位

AgentCell 是一个**本地优先、事件溯源、能力隔离、模型可替换、支持暂停恢复与多 Agent 协作的轻量级 Agent Runtime**。

它不是聊天机器人外壳，也不是教学 Demo。它应当能够安全、持续、可恢复地执行真实任务，并提供完整的审批、记忆、追踪、预算和多模型能力。

### 1.3 首发产品方向

首发版本聚焦于：

> **AI 软件项目执行工作台**

用户可将一个代码仓库作为工作区交给 AgentCell，由多个 Agent 协作完成：

- 分析项目结构与问题；
- 制定执行计划；
- 搜索、读取和修改代码；
- 运行测试、构建与静态检查；
- 生成补丁和变更说明；
- 在危险操作前请求用户审批；
- 对修改结果进行独立审查；
- 保存项目记忆和历史决策；
- 回放、恢复或分支历史运行。
- 通过统一自然语言任务入口自动选择合适的单 Agent 或确定性 Team，并允许高级用户显式覆盖。

系统架构必须保留扩展到研究分析、文件处理、业务自动化和数据分析的能力，但首版不得因追求通用性而牺牲代码质量和交付闭环。

---

## 2. 核心目标

AgentCell 必须完整体现以下能力：

1. Agent 推理与工具调用循环；
2. Function Calling 与结构化工具系统；
3. 多 Provider、多模型适配；
4. 短期记忆与长期记忆；
5. 上下文裁剪、压缩与摘要；
6. 工具权限、能力租约与人工审批；
7. 文本、工具和运行状态的流式输出；
8. 业务事件追踪与 OpenTelemetry 技术追踪；
9. 模型和工具调用的重试、超时与熔断；
10. 请求数、Token、费用、时间、步骤和子 Agent 数量限制；
11. 子 Agent 委派与多 Agent 协作；
12. CLI、HTTP API 和 React Web 工作台；
13. 运行暂停、恢复、取消、回放和分支；
14. SQLite 单机持久化；
15. 可测试、可替换、可扩展的 Provider、Tool、Memory 和 Storage 接口。
16. CLI 与 Web 共用的统一 Task Router，自然语言路由不得扩大权限或预算。

---

## 3. 非目标

首版明确禁止以下方向：

- 不做简单聊天套壳；
- 不把所有逻辑写入单个 `agent.py`；
- 不重复实现 PydanticAI 已稳定提供的底层模型协议；
- 不使用 LangChain 或 LangGraph 作为核心运行时；
- 不引入 Redis、Kafka、Celery 等分布式基础设施；
- 不引入独立 Node.js 后端；
- 不以向量数据库作为首版长期记忆前提；
- 不默认允许 Shell、删除文件或任意网络访问；
- 不允许子 Agent 获得超过父 Agent 的权限；
- 不把原始模型思维链作为产品功能或审计依据；
- 不为了“代码少”而删除测试、异常处理、迁移或安全边界；
- 不为了“功能全”而堆叠无实际使用场景的抽象层。

“轻量”指单进程、单数据库文件、少量核心依赖和清晰边界，不等于代码草率或功能残缺。

---

## 4. 设计原则

### 4.1 复用框架，不重复造轮子

PydanticAI 负责：

- 模型调用；
- Agent Loop 基础能力；
- Function Calling；
- 工具参数校验；
- 流式模型事件；
- 结构化结果；
- 基础重试；
- Usage 统计；
- Agent delegation 基础能力。

AgentCell 负责：

- 运行生命周期；
- Provider 工程；
- 事件溯源；
- 运行暂停与恢复；
- 检查点；
- 权限与审批；
- 能力租约；
- 记忆管理；
- 上下文压缩；
- 预算继承；
- 多 Agent 编排；
- HTTP、CLI 和 Web 产品接口。

### 4.2 事件优先

所有重要状态变化必须先形成领域事件，再由存储、SSE、日志和追踪消费者处理。

禁止在业务层直接拼装前端事件或直接依赖具体输出通道。

### 4.3 接口小而稳定

Provider、Tool、Memory、Policy、Storage 和 Event Sink 的接口必须小、明确、可替换。

不要为未来可能发生的需求提前增加多层抽象。只有存在两个实际实现或明确测试替身时，才提取协议。

### 4.4 安全默认拒绝

未声明的能力默认不可用。危险操作默认要求审批。路径、命令和网络访问必须经过明确限制。

### 4.5 单体优先

首版部署形态必须保持：

```text
一个 Python 服务
一个 SQLite 数据库
一个静态前端目录
一个工作区目录
```

### 4.6 可恢复而非仅可重试

模型请求失败可以重试；完整任务失败应尽量从最近检查点恢复，而不是无条件从头开始。

### 4.7 所有资源均有预算

模型、工具、子 Agent、时间、输出大小和存储均不得无限使用。

---

## 5. 技术栈

### 5.1 后端

- Python 3.12 或更高版本；
- `uv` 管理 Python 依赖和锁文件；
- FastAPI；
- Uvicorn；
- `pydantic-ai-slim`；
- Pydantic v2；
- pydantic-settings；
- HTTPX；
- SQLite；
- SQLAlchemy 2；
- Alembic；
- aiosqlite；
- Typer；
- Rich；
- Tenacity，仅在 PydanticAI 重试能力不足的边界层使用；
- OpenTelemetry；
- structlog 或标准库 logging，二选一，项目内只保留一套；
- pytest；
- pytest-asyncio；
- Ruff；
- Pyright。

### 5.2 前端

- React；
- TypeScript；
- Vite；
- pnpm；
- TanStack Query；
- Zustand，仅用于确有必要的跨页面本地状态；
- AG-UI 事件协议；
- SSE；
- Vitest；
- Playwright。

### 5.3 首版不引入

- Redis；
- PostgreSQL；
- Celery；
- Kafka；
- 独立 Node.js 服务；
- 向量数据库；
- 微服务框架；
- Kubernetes；
- 自研前后端流式事件协议。

---

## 6. 建议仓库结构

```text
agentcell/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── uv.lock
├── agentcell.toml
├── .env.example
├── alembic.ini
├── Dockerfile
├── compose.yaml
│
├── src/
│   └── agentcell/
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       ├── errors.py
│       │
│       ├── kernel/
│       │   ├── runtime.py
│       │   ├── run_service.py
│       │   ├── lifecycle.py
│       │   ├── checkpoint.py
│       │   └── replay.py
│       │
│       ├── agents/
│       │   ├── models.py
│       │   ├── registry.py
│       │   ├── factory.py
│       │   ├── delegation.py
│       │   └── builtins.py
│       │
│       ├── providers/
│       │   ├── base.py
│       │   ├── models.py
│       │   ├── factory.py
│       │   ├── bailian.py
│       │   └── deepseek.py
│       │
│       ├── tools/
│       │   ├── models.py
│       │   ├── registry.py
│       │   ├── workspace.py
│       │   ├── shell.py
│       │   ├── http.py
│       │   ├── memory.py
│       │   ├── artifacts.py
│       │   └── delegation.py
│       │
│       ├── policy/
│       │   ├── models.py
│       │   ├── engine.py
│       │   ├── capabilities.py
│       │   └── approvals.py
│       │
│       ├── memory/
│       │   ├── models.py
│       │   ├── service.py
│       │   ├── retrieval.py
│       │   ├── compaction.py
│       │   └── summarizer.py
│       │
│       ├── budgets/
│       │   ├── models.py
│       │   ├── tracker.py
│       │   └── pricing.py
│       │
│       ├── events/
│       │   ├── models.py
│       │   ├── bus.py
│       │   └── recorder.py
│       │
│       ├── storage/
│       │   ├── database.py
│       │   ├── tables.py
│       │   └── repositories.py
│       │
│       ├── telemetry/
│       │   ├── tracing.py
│       │   └── logging.py
│       │
│       ├── api/
│       │   ├── app.py
│       │   ├── dependencies.py
│       │   ├── schemas.py
│       │   └── routes/
│       │       ├── runs.py
│       │       ├── agents.py
│       │       ├── memory.py
│       │       └── system.py
│       │
│       └── cli/
│           ├── app.py
│           ├── chat.py
│           ├── run.py
│           └── inspect.py
│
├── migrations/
│   └── versions/
│
├── web/
│   ├── package.json
│   ├── pnpm-lock.yaml
│   ├── vite.config.ts
│   ├── tsconfig.json
│   └── src/
│       ├── main.tsx
│       ├── app.tsx
│       ├── api/
│       ├── components/
│       ├── hooks/
│       ├── pages/
│       └── types/
│
└── tests/
    ├── unit/
    ├── integration/
    ├── provider_contract/
    ├── replay/
    └── e2e/
```

不要仅为了匹配目录结构而创建空文件。模块在出现真实职责时再创建。

---

## 7. 依赖方向

允许的主要依赖方向：

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

约束如下：

- `kernel` 不得依赖 FastAPI、Typer 或 React 协议；
- `providers` 不得依赖 API 层；
- `tools` 不得直接访问全局数据库会话；
- `storage` 不得包含 Agent 决策逻辑；
- `api` 不得复制业务逻辑；
- `cli` 必须直接调用业务服务，不得通过 HTTP 请求本机 FastAPI；
- 前端不得依赖数据库结构；
- 领域模型与 ORM 模型必须分离。

---

## 8. Provider 工程

### 8.1 首批 Provider

首版支持：

- 阿里云百炼：`qwen3.7-plus`；
- DeepSeek 官方平台：`deepseek-v4-pro`。

后续新增模型必须通过 Provider 工程接入，不得在 Agent、Tool 或 API 代码中硬编码厂商判断。

### 8.2 配置示例

```toml
[models.qwen_plus]
provider = "bailian"
model = "qwen3.7-plus"
api_key_env = "DASHSCOPE_API_KEY"
thinking = true
thinking_budget = 12000
max_output_tokens = 16000
timeout_seconds = 120
max_retries = 3

[models.deepseek_pro]
provider = "deepseek"
model = "deepseek-v4-pro"
api_key_env = "DEEPSEEK_API_KEY"
thinking = true
reasoning_effort = "high"
max_output_tokens = 16000
timeout_seconds = 120
max_retries = 3
```

### 8.3 Provider 规则

必须：

- 使用统一 `ModelSpec`；
- 由 `ProviderFactory` 创建模型实例；
- 将厂商专有参数限制在对应适配器中；
- 支持注入自定义 HTTPX Client；
- 支持独立超时、代理和连接池；
- 对认证错误、限流、超时和上游错误分类；
- 记录 Provider、模型、延迟和 Usage；
- 提供离线 Fake Provider；
- 编写 Provider Contract Tests。

禁止：

- 在 Agent 代码中判断 `provider == "bailian"`；
- 把 API Key 写入配置文件或数据库明文字段；
- 将完整请求头或密钥记录到日志；
- 默认在 Provider 之间静默降级；
- 在未记录事件的情况下自动切换模型。

### 8.4 子 Agent 模型配置

每个 Agent 必须可独立指定：

- Provider；
- 模型；
- 思考模式；
- 最大输出 Token；
- 温度；
- 超时；
- 重试次数；
- 预算；
- 工具集合。

父 Agent 和子 Agent 不要求使用相同模型。

---

## 9. Agent 定义与运行实例

Agent 声明应为无状态配置：

```python
class AgentSpec(BaseModel):
    id: str
    name: str
    description: str
    model_ref: str
    instructions: str
    tools: list[str]
    capabilities: list[str]
    max_steps: int = 20
    max_children: int = 3
    max_depth: int = 2
```

运行时状态通过依赖对象注入：

```python
@dataclass
class RunDeps:
    run_id: UUID
    conversation_id: UUID
    user_id: UUID
    workspace: Path
    memory: MemoryService
    events: EventBus
    policy: PolicyEngine
    budget: BudgetTracker
    agents: AgentRegistry
```

禁止将用户会话、当前运行 ID、预算或工作区保存到全局 Agent 单例。

---

## 10. Agent 运行循环

AgentCell 不重复实现完整模型协议，但必须显式控制运行生命周期。

每次运行至少包含：

1. 创建 Run；
2. 记录 `run.started`；
3. 构建上下文；
4. 调用模型；
5. 流式转发文本和工具事件；
6. 校验工具调用；
7. 执行能力检查；
8. 必要时创建审批并暂停；
9. 执行工具；
10. 保存工具结果或 Artifact；
11. 更新预算；
12. 必要时压缩上下文；
13. 必要时委派子 Agent；
14. 创建检查点；
15. 完成、失败、取消或等待审批；
16. 记录最终事件和统计。

运行状态至少包括：

```text
created
running
waiting_approval
paused
completed
failed
cancelled
```

所有状态转换必须在单一生命周期模块中定义，禁止任意模块直接修改状态字符串。

---

## 11. 事件溯源与检查点

### 11.1 核心事件

至少实现：

```text
run.started
run.status_changed
model.requested
model.text_delta
model.completed
model.failed
tool.proposed
tool.approval_required
tool.approved
tool.rejected
tool.started
tool.output_delta
tool.completed
tool.failed
agent.child_started
agent.child_completed
memory.recalled
memory.written
context.compacted
budget.updated
checkpoint.created
task.route_proposed
task.route_confirmed
task.route_overridden
task.route_rejected
file.change_prepared
file.change_applied
file.change_completed
file.change_conflict
file.change_reverted
run.completed
run.failed
run.cancelled
```

### 11.2 事件规则

- 事件表只追加，不更新历史事件；
- 每个 Run 内事件必须有单调递增的 `sequence`；
- 事件 payload 必须使用版本化 Pydantic 模型；
- 大型内容必须存入 Artifact Store，事件中只保留引用；
- 敏感参数必须脱敏；
- 前端 SSE 事件由领域事件映射而来；
- OpenTelemetry Span 不能替代业务事件。

### 11.3 检查点

检查点至少保存：

- 当前 Agent；
- 消息历史；
- 运行状态；
- 未完成审批；
- 预算快照；
- 父子 Run 关系；
- 相关 Artifact；
- 可恢复的 Provider 上下文。

检查点必须支持：

- 审批后恢复；
- 进程重启后恢复；
- 历史 Run 回放；
- 从指定事件序号创建分支。

---

## 12. 工具系统

### 12.1 首版 Toolset

```text
workspace
  list
  read
  search
  write
  patch
  delete

shell
  run
  test

memory
  search
  remember
  forget

agent
  delegate
  status

http
  request

artifact
  save
  load
```

### 12.2 工具元数据

每个工具必须声明：

```python
class ToolPolicy(BaseModel):
    risk: RiskLevel
    requires_approval: bool
    idempotent: bool
    timeout_seconds: float
    max_output_bytes: int
    capabilities: set[str]
```

### 12.3 工具执行规则

必须：

- 使用结构化参数；
- 在执行前进行 Pydantic 校验；
- 校验能力租约；
- 校验路径与网络范围；
- 设置超时；
- 捕获并分类异常；
- 对输出进行大小限制；
- 记录开始、完成和失败事件；
- 将大型输出保存为 Artifact；
- 仅对幂等工具自动重试。

禁止：

- 使用 `shell=True` 拼接未经处理的用户输入；
- 允许路径逃逸工作区；
- 将整个环境变量集合提供给模型；
- 对删除、发送、支付等非幂等操作自动重试；
- 将二进制或超大文件直接放入模型上下文。

---

## 13. 权限、能力租约与审批

### 13.1 风险等级

```text
SAFE
GUARDED
DANGEROUS
FORBIDDEN
```

示例：

- `SAFE`：读取文件、搜索文本；
- `GUARDED`：写文件、HTTP POST；
- `DANGEROUS`：删除文件、执行命令、Git push；
- `FORBIDDEN`：读取密钥、越权路径、未授权域名。

### 13.2 能力租约

每个 Run 使用 `CapabilityLease`：

```python
class CapabilityLease(BaseModel):
    filesystem_read: list[str]
    filesystem_write: list[str]
    network_domains: list[str]
    commands: list[str]
    can_delegate: bool
    max_child_depth: int
```

子 Agent 的能力必须是父 Agent 能力的子集。

### 13.3 审批内容

审批请求至少展示：

- Agent 名称；
- Provider 和模型；
- 工具名称；
- 参数；
- 风险等级；
- 预计影响；
- 文件 Diff；
- 剩余预算；
- 是否幂等；
- 超时设置。

审批结果支持：

- 批准；
- 拒绝；
- 修改参数后批准；
- 对当前 Run 的同类操作临时批准。

不得默认提供永久全局批准。

### 13.4 CLI 权限模式

CLI 必须将 AgentSpec、CapabilityLease 和审批模式作为三个独立边界：AgentSpec 定义可用工具上限，CapabilityLease 定义当前 Run 的路径、命令和网络范围，审批模式只决定租约内操作何时需要人工决定。

首版 CLI 权限模式包括：

- `request`（请求审批）：GUARDED 和 DANGEROUS 操作逐次请求用户审批；
- `auto`（替我审批）：仅由确定性的 PolicyEngine 自动批准租约内 GUARDED 操作，DANGEROUS 仍请求用户审批；
- `full`（当前工作区完全访问）：PolicyEngine 可自动批准显式租约内 GUARDED 和 DANGEROUS 操作，但仍必须完整记录决定、事件、检查点和执行账本。

权限模式不得让模型成为审批主体，不得为 AgentSpec 增加工具，不得扩大 CapabilityLease。任何模式下，FORBIDDEN、敏感路径、工作区逃逸、未授权命令、SSRF、预算超限和安全校验失败都必须拒绝。`full` 不等于任意系统访问，不得隐式允许 `*` 命令、`shell=True`、永久全局批准或绕过非幂等防重放。

---

## 14. 多 Agent 协作

首版实现两种协作方式：

### 14.1 Agent as Tool

父 Agent 调用子 Agent，等待其返回结构化结果。

适用于研究、编码、测试、审查和总结。

### 14.2 程序化 Handoff

应用根据结构化状态切换 Agent。

适用于：

```text
Coordinator → Coder → Reviewer → Finalizer
```

### 14.3 预算继承

子 Agent 不得获得独立无限预算。

父 Run 必须从剩余预算中分配子预算，包括：

- 请求数；
- Token；
- 费用；
- 工具调用数；
- 最大持续时间；
- 最大子 Agent 数；
- 最大深度。

### 14.4 首版内置 Agent

建议提供：

- `coordinator`：任务分析、规划、委派和整合；
- `coder`：代码修改和测试；
- `reviewer`：只读审查、安全检查和回归分析；
- `researcher`：资料检索和证据整理；
- `summarizer`：低成本上下文摘要。

`reviewer` 默认不得拥有写权限。

### 14.5 统一任务入口与路由

CLI 和 Web 的普通用户入口必须允许只提交自然语言任务，由同一个 transport-neutral `TaskRoutingService` 在已注册 Agent 和 Team 中选择执行方式。首版候选至少包括只读 Coordinator、Coder、Reviewer、Researcher 和确定性的 `software` Team；显式 `--agent`、`--team`、模型和预算参数继续作为高级用户与自动化脚本的覆盖入口。

路由结果必须是结构化、可校验和可审计的数据，至少包含：

- `single_agent` 或 `team` 执行模式；
- Agent ID 或 Team ID；
- 面向用户的简短选择依据和置信度；
- 预计需要的能力、审批模式和预算摘要；
- 是否需要用户确认或补充信息。

路由器必须遵守：

- 只选择执行方案，不创建新工具、不扩大 AgentSpec、CapabilityLease、预算或网络范围；
- 优先使用确定性规则处理明确意图，只有歧义任务才使用模型结构化分类；
- 低置信度、多个高风险候选或需要额外能力时必须请求用户确认，不得静默猜测；
- 显式 Agent/Team 覆盖优先于自动路由，但仍必须经过 Registry、Policy、Lease 和 Budget 校验；
- 路由决定、用户覆盖、确认和最终执行身份必须持久化为领域事件并可回放；
- 实际任务必须先创建 root Run，再在该 Run 上记录路由事件并把路由模型 Usage 计入预算；无 Run 的 dry-route 只是非权威预览，不得执行工具，并必须明确可能产生的有界模型 Token/费用；
- 路由输入只包含用户任务、可公开的工作区元数据、Registry 和当前安全 profile；不得为了分类先读取任意文件或执行工具，需要检查项目后才能决定时先路由到只读 Coordinator；
- 不保存或展示路由模型的原始思维链，只保留安全、简短的选择依据；
- `inspect`、`resume`、`changes`、`agents`、`serve` 等管理操作继续使用显式命令，不交给自然语言路由器执行。

---

## 15. 记忆系统

### 15.1 四层记忆

1. Working Memory：当前 Run 的计划、阶段和临时状态；
2. Conversation Memory：完整有序的消息与工具历史，以及由其派生的可检索投影；
3. Episodic Memory：某次任务的摘要、结果和经验；
4. Semantic Memory：用户偏好、项目规则和稳定事实。

### 15.2 Conversation 与多轮上下文

- Conversation 是多个有序 Run 的容器，每次用户追问必须创建新的 Run；
- 已完成的 Run 不得通过 `resume` 伪装成下一轮对话；
- 新 Run 应加载同一 Conversation 中经过裁剪、压缩和脱敏的历史消息；
- 完整有序的会话消息线程是权威记录，FTS 检索投影不能替代它；
- 跨回合不得自动继承上一 Run 的剩余预算、临时审批或扩大后的能力租约；
- 同一 Conversation 默认只允许一个活动 Run，冲突创建必须明确拒绝；
- 用户、项目、工作区和执行作用域必须校验，禁止跨作用域拼接历史；固定 Agent/Team Conversation 必须保持绑定，auto-routing Conversation 只能在版本化 RoutingPolicy 允许的 public Agent/Team 集合中切换，并为每个 Run 保存实际执行身份；
- 工具调用与工具结果跨 Run 注入时仍必须成对保留，大型内容继续使用 Artifact 引用；
- 不持久化或回传原始模型思维链。

### 15.3 首版检索

首版使用 SQLite FTS5，不要求向量数据库。

检索至少考虑：

- BM25；
- 时间衰减；
- 重要度；
- 用户作用域；
- 项目作用域；
- Agent 作用域；
- 标签匹配。

### 15.4 记忆写入

模型可提出记忆候选，但 Memory Policy 决定：

- 是否保存；
- 保存类型；
- 作用域；
- 去重；
- 敏感信息处理；
- 过期时间；
- 是否需要用户批准。

用户必须能够查看、编辑和删除长期记忆。

---

## 16. 上下文管理与压缩

上下文构建顺序建议为：

1. 系统指令；
2. Agent 角色指令；
3. 工作区和能力说明；
4. 当前任务状态；
5. 最近消息；
6. 相关长期记忆；
7. 压缩后的历史摘要；
8. 工具定义。

至少实现：

- `PairSafeTrimmer`；
- `ToolOutputCompactor`；
- `EpisodicSummarizer`；
- `MemoryInjector`。

工具调用与工具结果必须成对保留。

超过阈值的工具输出必须替换为 Artifact 摘要和引用。

摘要任务优先使用低成本、低温度、关闭深度思考的模型配置。

---

## 17. 预算、重试与超时

### 17.1 默认预算

每个 Run 至少限制：

```python
class Budget(BaseModel):
    max_requests: int
    max_input_tokens: int
    max_output_tokens: int
    max_total_tokens: int
    max_tool_calls: int
    max_duration_seconds: int
    max_cost: Decimal | None
    max_children: int
    max_depth: int
```

### 17.2 Provider 重试

可重试：

- 连接失败；
- 读取超时；
- HTTP 429；
- HTTP 502、503、504。

默认不可重试：

- 401；
- 403；
- 无效模型名；
- 无效工具 Schema；
- 参数校验错误；
- 明确的上下文超限。

### 17.3 工具重试

仅 `idempotent=True` 的工具允许自动重试。

### 17.4 预算事件

每次模型调用、工具调用和子 Agent 委派后必须更新预算并记录 `budget.updated`。

达到限制时，Run 应以明确错误完成，不得静默截断。

---

## 18. SQLite 持久化

核心表建议包括：

```text
agents
model_specs
conversations
runs
run_events
messages
tool_calls
approvals
memory_items
artifacts
checkpoints
change_sets
file_changes
```

SQLite 初始化必须启用：

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

要求：

- 所有 Schema 变更必须通过 Alembic；
- 禁止应用启动时临时修改生产表结构；
- Repository 返回领域模型，不直接泄露 ORM 实例；
- JSON 字段必须有 Pydantic Schema；
- 时间统一保存为 UTC；
- 主键优先使用 UUID；
- 事件序号必须有唯一约束；
- 测试使用独立临时数据库。

---

## 19. HTTP API 与流式协议

### 19.1 核心接口

```text
POST   /api/runs
GET    /api/runs/{run_id}
GET    /api/runs/{run_id}/events
POST   /api/runs/{run_id}/cancel
POST   /api/runs/{run_id}/resume
POST   /api/runs/{run_id}/branch

POST   /api/conversations
GET    /api/conversations
GET    /api/conversations/{conversation_id}
GET    /api/conversations/{conversation_id}/messages
POST   /api/conversations/{conversation_id}/runs

POST   /api/task-routes
POST   /api/tasks

GET    /api/agents
POST   /api/agents
PUT    /api/agents/{agent_id}

GET    /api/providers
GET    /api/tools

GET    /api/memories
DELETE /api/memories/{memory_id}

GET    /api/health
GET    /api/version
```

### 19.2 流式输出

优先使用 AG-UI 与 SSE。

前端至少接收：

- 文本增量；
- 工具提议；
- 审批请求；
- 工具状态；
- 子 Agent 状态；
- 上下文压缩；
- 记忆召回；
- 预算更新；
- Run 完成或失败。

WebSocket 仅在未来实现交互式 PTY 时引入。

### 19.3 API 规则

- API Schema 必须使用 Pydantic；
- 错误返回统一 Problem Details 或统一错误模型；
- API 路由不得包含业务编排；
- Task Router 必须位于应用服务层并由 CLI/Web 共用；`POST /api/task-routes` 返回不创建 Run、不执行工具的非权威预览，`POST /api/tasks` 先创建 root Run，再把路由 Usage、决定和覆盖持久化后启动执行；
- 路由请求可携带显式 Agent/Team 覆盖，但不得由客户端声明超过服务端 AgentSpec、Lease 或 Budget 上限的权限；
- Run 创建必须返回稳定 ID；
- Conversation 中的新回合必须创建新 Run，并按消息序号加载有界历史；
- 同一 Conversation 的并发活动 Run 必须返回冲突；
- 恢复和分支操作必须幂等或明确返回冲突；
- 敏感配置不得通过 API 返回。

---

## 20. CLI

CLI 至少支持：

```bash
agentcell chat
agentcell run "分析当前项目"
agentcell run --agent coordinator "修复测试失败"
agentcell run --agent coder --permission-mode request "修复测试失败"
agentcell chat --agent coder --permission-mode request
agentcell run inspect <run-id>
agentcell run replay <run-id>
agentcell run branch <run-id> --from-sequence 18
agentcell run cancel <run-id>
agentcell agents list
agentcell tools list
agentcell memory search "数据库设计"
agentcell changes list --run <run-id>
agentcell changes show <change-id>
agentcell changes diff <change-id>
agentcell changes revert <change-id>
agentcell serve
```

CLI 必须：

- 直接调用 `RunService`；
- 支持 Rich 流式输出；
- `run` 和新建 `chat` 支持显式选择 Agent；继续已有 Conversation 时不得静默更换固定 Agent；
- 普通任务允许只传自然语言输入，由统一 Task Router 自动选择单 Agent 或确定性 Team；显式 `--agent`/`--team` 是高级覆盖，不应成为普通用户理解系统内部角色的前提；
- 支持显式、可审计的写路径和命令租约参数，默认 coordinator 保持只读；
- 支持 `request`、`auto` 和 `full` 三种 CLI 权限模式，并遵循 13.4 的不可突破边界；
- 正确处理 Ctrl+C 并取消 Run；
- 对审批提供交互式选择；
- `agentcell chat` 必须在同一 Conversation 中连续创建 Run，并支持退出后按 ID 继续；
- `agentcell run` 保持单任务入口，不得静默继承其他 Run 的历史；
- 支持 JSON 输出，便于脚本集成；
- Chat 每轮完成行同时显示 Run ID 和 Conversation ID，退出时打印可复制的继续命令；
- 流式界面展示文本、工具、审批、预算和公开进度摘要，但不得显示原始模型思维链；
- 返回有意义的进程退出码。

---

## 21. React Web 工作台

首版页面包括：

1. 任务与会话工作台；
2. 会话消息线程和连续追问；
3. 审批中心；
4. 运行时间线；
5. Agent 管理；
6. 记忆管理；
7. Provider 与模型状态；
8. Token、费用和耗时统计。

前端规则：

- 服务端状态使用 TanStack Query；
- 任务工作台以一个自然语言输入框作为主要入口，默认展示后端路由建议；Agent/Team 选择器作为可展开的高级覆盖，不在 React 中实现第二套路由器；
- 执行前应显示选中的 Agent/Team、能力范围、审批方式和预算摘要；低置信度或需要新增能力时要求用户确认；
- 不复制后端预算、权限或状态机逻辑；
- SSE 断线后必须支持按事件序号续传；
- 工具参数和 Diff 需要安全展示；
- Markdown 和代码块必须防止 XSS；
- 不在 localStorage 保存 API Key；
- 所有审批操作必须有明确反馈；
- 长列表使用虚拟化或分页；
- 组件只接收稳定前端 DTO，不直接依赖后端 ORM 字段。

---

## 22. 日志与追踪

采用双层可观测性：

### 22.1 业务事件

SQLite `run_events` 用于：

- 时间线；
- 审计；
- 回放；
- 恢复；
- 分支；
- 产品统计。

### 22.2 OpenTelemetry

用于：

- Provider 请求延迟；
- 首 Token 时间；
- Token 使用；
- 工具耗时；
- 重试次数；
- 错误堆栈；
- 父子 Agent Trace；
- HTTP 请求链路。

日志必须结构化，并包含：

```text
trace_id
run_id
conversation_id
agent_id
provider
model
event_type
```

禁止记录：

- API Key；
- Authorization Header；
- 完整环境变量；
- 未脱敏的敏感工具参数；
- 默认情况下的完整原始模型推理内容。

---

## 23. 安全要求

### 23.1 文件系统

- 所有路径在解析后必须位于工作区内；
- 禁止符号链接逃逸；
- 写入前生成 Diff；
- 删除操作必须审批；
- 默认禁止访问 `.env`、密钥目录和版本控制凭据；
- 大文件读取必须分块。
- 每次已批准的创建、替换、补丁和删除必须保存可校验的 before/after 哈希、Artifact 和完整 Diff，并关联 Run、工具调用和审批；
- 修改历史必须在非 Git 工作区仍可查询和恢复；Git 只提供 HEAD、dirty 状态和 path-scoped Diff 增强，不能替代 AgentCell ChangeSet/FileChange 账本；
- 回滚必须创建新的受审批反向变更，并以当前哈希匹配原 after 哈希为前提，不得删除或改写原审计历史；
- 文件系统与 SQLite 间的崩溃窗口必须通过 prepared/applied/completed/conflict 状态和哈希对账恢复，不得盲目重放非幂等写入。

### 23.2 Shell

- 默认关闭；
- 仅允许白名单命令；
- 参数数组执行，禁止字符串拼接；
- 设置工作目录、超时、输出限制和环境白名单；
- 高风险命令必须拒绝或审批；
- 首版不承诺提供强隔离，文档必须明确这一点。
- Git 检查仅允许受控只读 argv、path-scoped 输出、清洗环境、超时和大小限制；不得让模型读取 `.git`，不得在自动回滚中执行 `reset --hard`、全工作区 checkout/restore、clean、stash、commit 或 push。

### 23.3 网络

- 使用域名白名单；
- 限制协议为 HTTPS，开发环境例外；
- 设置超时和响应大小限制；
- 禁止访问云元数据地址和本机敏感端口；
- 对 POST、PUT、PATCH、DELETE 默认审批。

### 23.4 密钥

- 密钥仅从环境变量或 Secret Resolver 读取；
- 不持久化明文密钥；
- 不将密钥传入模型上下文；
- 不在异常和日志中输出密钥。

---

## 24. 编码规范

### 24.1 Python

- 所有公共函数必须有类型标注；
- 使用 `from __future__ import annotations`；
- 优先使用 Pydantic 模型表达跨边界数据；
- 内部轻量状态可使用 dataclass；
- 异步 I/O 使用 `async`；
- 禁止阻塞调用进入事件循环；
- 异常必须使用项目自定义错误层次；
- 不使用裸 `except Exception` 吞掉错误；
- 不使用可变对象作为默认参数；
- 业务代码避免全局单例；
- 单个函数应保持单一职责；
- 优先组合，不使用深继承树；
- 代码必须通过 Ruff 和 Pyright。

### 24.2 TypeScript

- 开启严格模式；
- 禁止无理由使用 `any`；
- API 类型应由 OpenAPI 生成或集中定义；
- 组件 props 必须显式类型化；
- 异步请求必须处理 loading、error 和 cancellation；
- 不在组件中复制领域状态机；
- 代码必须通过 ESLint、TypeScript 和测试。

### 24.3 注释与文档

注释解释“为什么”，不要复述代码。

新增以下内容时必须更新文档：

- Provider；
- 环境变量；
- 数据库迁移；
- CLI 命令；
- HTTP API；
- Toolset；
- 权限模型；
- 配置格式。

---

## 25. 测试策略

### 25.1 单元测试

覆盖：

- 预算计算；
- 状态转换；
- 能力租约子集校验；
- 路径安全；
- 工具参数校验；
- 上下文裁剪；
- 记忆评分；
- Provider 参数映射；
- 事件序号；
- 审批决策。

### 25.2 集成测试

覆盖：

- Run 完整生命周期；
- 工具调用；
- 审批暂停与恢复；
- 子 Agent 委派；
- 上下文压缩；
- SQLite 持久化；
- 进程重启后恢复；
- 同一 Conversation 的多轮消息继承与并发冲突；
- 跨用户、项目、工作区和 Agent 的会话隔离；
- SSE 事件顺序；
- CLI 与 RunService。
- Task Router 的确定性规则、结构化模型回退、显式覆盖、权限差额确认与 CLI/API 一致性。

### 25.3 Provider Contract Tests

对百炼和 DeepSeek 分别验证：

- 普通文本输出；
- 流式输出；
- Function Calling；
- 多轮工具调用；
- Usage 字段；
- 思考模式参数；
- 超时；
- 429 重试；
- 无效密钥；
- 上下文超限。

真实 Provider 测试必须通过环境变量显式开启，默认测试套件不得消耗线上额度。

### 25.4 回放测试

固定 Fake Provider 响应和事件序列，验证：

- 同一事件流得到同一最终状态；
- 检查点恢复不重复执行已完成的非幂等工具；
- 分支 Run 正确继承指定序号前的状态；
- 审批后恢复不会丢失工具调用关联。

### 25.5 E2E

至少覆盖：

- 创建任务；
- 接收流式文本；
- 看到工具调用；
- 批准文件修改；
- 查看运行时间线；
- 取消任务；
- 恢复任务；
- 在同一 Conversation 中连续追问并引用上一轮结果；
- 重启服务后继续既有 Conversation；
- 查看长期记忆。
- 只输入自然语言任务后得到路由预览，并安全启动匹配的单 Agent 或 software Team；低置信度和能力差额必须确认。

---

## 26. 本地开发命令

命令名称可在项目初始化时微调，但必须保持统一入口。

```bash
uv sync --all-extras
uv run alembic upgrade head
uv run agentcell serve
uv run agentcell chat

uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest

cd web
pnpm install
pnpm dev
pnpm test
pnpm build
```

推荐在 `pyproject.toml` 或 `Makefile` 中提供：

```bash
make dev
make test
make lint
make format
make build
```

---

## 27. 开发 Agent 的工作方式

执行任何代码任务时，遵循以下顺序：

1. 阅读根目录及目标目录中的 `AGENTS.md`；
2. 检查现有代码、测试和配置；
3. 确认变更所属模块及依赖方向；
4. 先给出最小可行实现方案；
5. 优先修改现有模块，不创建重复抽象；
6. 同步补充或更新测试；
7. 执行与改动相关的最小测试集；
8. 再执行静态检查和完整测试；
9. 检查迁移、文档和配置是否需要更新；
10. 汇报变更、测试结果、风险和未完成项。

不得：

- 未阅读相关代码就大规模重写；
- 为通过测试删除安全校验；
- 伪造测试通过；
- 留下无说明的 TODO；
- 在没有测试的情况下修改状态机、预算、审批或恢复逻辑；
- 将临时调试输出提交到生产代码；
- 顺手修改与任务无关的文件；
- 未经说明改变公开 API 或配置格式。

---

## 28. 变更提交要求

每次功能变更应尽量保持垂直闭环，包括：

- 领域模型；
- 业务实现；
- 存储；
- API 或 CLI；
- 测试；
- 文档。

提交信息建议使用 Conventional Commits：

```text
feat(kernel): add resumable approval checkpoints
fix(provider): map deepseek timeout errors correctly
test(replay): cover non-idempotent tool recovery
docs(config): document qwen model settings
```

数据库迁移必须单独审查，不应混入大量无关格式化变更。

---

## 29. 完成定义

一个任务只有同时满足以下条件才算完成：

- 功能符合需求；
- 未破坏依赖方向；
- 有必要的错误处理；
- 有预算和超时边界；
- 涉及危险操作时有权限校验；
- 关键状态变化有事件；
- 需要恢复时有检查点；
- 有自动化测试；
- 相关测试通过；
- Ruff、Pyright 和前端类型检查通过；
- 数据库变更有迁移；
- 配置和文档已更新；
- 日志中不存在密钥或敏感数据；
- 未引入无必要的大型依赖；
- 最终变更说明准确。

---

## 30. 首版里程碑

### M1：可运行微内核

- ProviderFactory；
- Qwen 与 DeepSeek；
- AgentFactory；
- RunService；
- ToolRegistry；
- SQLite EventStore；
- 预算；
- CLI；
- Fake Provider 测试。

### M2：生产执行能力

- 工作区工具；
- Shell 安全边界；
- 审批暂停与恢复；
- Capability Lease；
- 短期记忆；
- 长期记忆；
- 上下文压缩；
- 子 Agent；
- 检查点；
- OpenTelemetry。

### M3：产品接口

- FastAPI；
- AG-UI/SSE；
- React 工作台；
- 审批中心；
- 运行时间线；
- Agent 管理；
- 记忆管理；
- 成本和预算视图。

### M4：可扩展生态

- MCP Client；
- 新 Provider；
- Embedding Retriever；
- PostgreSQL Adapter；
- 强隔离 Sandbox；
- 多用户认证；
- 固定工作流 Graph。

---

## 31. 最终架构判断标准

任何新增设计都必须回答以下问题：

1. 是否减少重复代码或解决真实需求？
2. 是否保持核心运行时可测试？
3. 是否保持 Provider、Tool 和 Storage 可替换？
4. 是否破坏单体部署能力？
5. 是否扩大了默认权限？
6. 是否有明确预算和超时？
7. 是否可被事件记录和恢复？
8. 是否值得增加新的依赖或抽象？

若无法给出明确正面答案，应选择更简单的实现。

---

## 32. 项目精神

AgentCell 的目标不是用最少文件做出看似聪明的 Demo，而是：

> 用尽可能少而清晰的代码，构建一个真正具备安全执行、长期记忆、多人机协作、模型可替换、过程可审计和故障可恢复能力的 Agent Runtime。

所有代码和设计都应服务于这个目标。
