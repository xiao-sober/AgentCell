# Agent 与多 Agent 协作现状

本文说明 AgentCell 当前注册的 Agent、实际可用边界、模型来源，以及 CLI/Web 多 Agent 产品化目标。Agent 是无状态 `AgentSpec`；只有创建 Run 后才产生运行实例、预算、能力租约、事件和检查点。

## 1. 当前内置 Agent

| ID | 产品可见性目标 | 职责 | 工具与能力上限 | 步骤/委派上限 |
|---|---|---|---|---|
| `assistant` | public | 普通问答与产品身份说明 | 无工具、无能力 | `max_steps=4`；不能委派，不得声称检查或修改工作区 |
| `coordinator` | public | 分析、规划和整合 | 普通应用组合只含 workspace list/read/search | `max_steps=20`、`max_children=0`、`max_depth=0`；不委派 |
| `coder` | public | 修改代码并运行批准的检查 | workspace list/read/search/write/patch/delete，`shell.test` | `max_steps=30`；不能委派 |
| `reviewer` | public | 独立只读审查、回归和安全分析 | workspace list/read/search | `max_steps=20`；结构性只读，不能委派 |
| `researcher` | public | 工作区与批准网络范围内的证据研究 | workspace list/read/search，`http.request` | `max_steps=20`；没有域名租约时 HTTP 不可见，不能委派 |
| `summarizer` | internal | 低成本、有界上下文摘要 | 无工具 | `max_steps=5`；不能委派 |
| `finalizer` | internal | 根据阶段证据生成最终交接 | workspace list/read/search | `max_steps=15`；只读，不能委派 |

工具集合是 AgentSpec 上限，不等于本次 Run 已获得权限。最终有效工具始终是 AgentSpec、CapabilityLease、Tool 参数安全校验和预算的交集；审批模式只决定已授权操作如何取得批准。

## 2. `agents list` 当前语义

当前：

```powershell
uv run agentcell agents list
```

展示的是应用本次启动后合并得到的有效 AgentRegistry。未传模型时，内置 Agent 使用 `agentcell.toml` 中第一个模型；当前示例配置第一个是 `qwen_plus`。列表用 `configured=<model_ref>` 明确表示当前组合的配置预览，不把它描述成历史 Run 或 Conversation 的实际身份；新 Run 会保存执行身份快照，新 Conversation 还会持久化绑定 `model_ref`，续聊不读取 Registry 默认模型覆盖该绑定。

应用随后读取数据库中的持久化 AgentSpec：同 ID 记录替换内置定义，新 ID 记录追加。Registry 在 AgentSpec 之外保存独立元数据并区分：

- `builtin`：纯内置定义；
- `persisted`：数据库新增定义；
- `override`：数据库同 ID 定义覆盖内置 Agent；
- `public/internal`：普通用户入口与内部运行角色。

默认人类列表只展示 public Agent，`--all` 或 `--json` 可查看 internal summarizer/finalizer。默认行包含 ID、来源、当前配置模型、访问摘要和状态；`--verbose` 增加名称、visibility、tools、capabilities、步骤和子 Agent 上限。例如：

```text
coder        builtin    configured=qwen_plus  access=read,write,shell  available
coordinator  builtin    configured=qwen_plus  access=read              available
reviewer     builtin    configured=qwen_plus  access=read              available
```

`--json` 稳定返回完整 AgentSpec，并增加 `source`、`visibility`、`status`、`configured_model_ref` 和访问摘要；JSON 自动包含 internal 角色。默认表保持简洁，完整字段进入 `--verbose`/`--json`。HTTP `GET /api/agents` 默认同样只返回 public Agent，显式 `include_internal=true` 才包含内部角色。

## 3. 当前单 Agent 与多 Agent 行为

普通 CLI Run 当前是单 Agent：

```text
agentcell run --agent coder       → 只运行 Coder
agentcell run --agent reviewer    → 只运行 Reviewer
agentcell run --agent coordinator → 通常仍只运行 Coordinator
```

后端已经实现两种多 Agent 基础：

1. Agent as Tool：collaborative coordinator 可调用 `agent.delegate`，创建持久化子 Run，父 Run 等待/恢复，并继承父预算和租约上限；
2. 程序化 Handoff：Kernel 已有 `Coordinator → Coder → Reviewer → Finalizer` 的确定性、检查点化流水线。

普通单 Agent CLI 仍保持 `max_children=0/max_depth=0`，不会自主委派；阶段 9.3 仅通过显式 `agentcell run --team software` 接通程序化 Handoff。Coder、Reviewer、Researcher、Summarizer 和 Finalizer 均没有 `agent.delegate`，不会自行调用其他 Agent。

## 4. 阶段 9.3 CLI 多 Agent（已实现）

首个 CLI 多 Agent 产品入口只开放确定性 `software` Team，不同时开放模型自主委派：

```powershell
# 固定、可预测的软件交付流水线
uv run agentcell run --team software --model-ref deepseek_pro --approval-mode request "修复测试并独立审查"
```

- `--team software` 使用配置化的 Coordinator→Coder→Reviewer→Finalizer Handoff；它与显式 `--agent` 互斥，阶段、子预算、模型和租约来自版本化 TeamSpec；
- 默认 root 预算为 24 次模型请求和 48 次工具调用；Coordinator 保持规划层并限制探索范围，Coder 获得主要工具额度，Reviewer 只读，Finalizer 无工具且只消费持久化阶段证据；
- Agent-as-Tool 后端继续保留和测试，但自主 `--collaborate` 不作为阶段 10 门禁；只有确定性 Team 的成本、审批和恢复边界稳定后才产品化；
- 子 Agent 的 CapabilityLease 必须是父租约子集，预算从父剩余量预留；子审批、取消、失败、恢复和重放必须正确传播；
- CLI 展示父/子阶段事件和审批状态，完成行输出 root Run ID、child Run IDs 和 Conversation ID；失败结果同时给出阶段、稳定错误码和公开错误原因；
- 不提供“任意无限子 Agent”或隐式跨 Provider 降级。不同 Agent 可显式使用不同模型，但每次选择都必须持久化并有事件记录。

## 5. 阶段 9.4 统一任务入口

阶段 9.4 已完成路由领域模型、确定性规则、有界结构化模型回退、安全校验、任务 root、路由事件、确认/拒绝检查点和实际 child 执行。single Agent 与 software Team 都复用既有委派/Handoff，在同一任务 root 下结算预算、审批与终态，并可跨进程恢复。auto chat 的普通问答在应用服务层确定性识别后直接运行 tool-free Assistant，不创建 task-router root 或 child；工作区任务仍按版本化 RoutingPolicy 路由。routed single-Agent child 的公开文本继续以标准 `model.text_delta` 实时投影到 root；single-Agent 和 Team child 的工具生命周期也按来源 sequence 投影，动态框可显示执行 Agent、工具名和白名单参数摘要，恢复后不重复且不复制工具输出内容。

普通用户不应被要求先理解六个 Agent、Team、Handoff 或委派预算。阶段 9.4 在 CLI 和 Web 之间建立共用的 `TaskRoutingService`：

```text
自然语言任务
    ↓
Task Router（确定性规则优先，歧义时结构化模型回退）
    ↓
coordinator / coder / reviewer / researcher / software Team
    ↓
Policy + Lease + Budget 校验与必要确认
    ↓
实际 Run / child Runs
```

- `agentcell run "..."` 和 `agentcell chat` 默认走统一路由，不要求普通用户传 `--agent` 或 `--team`；
- `--agent`、`--team`、模型和预算继续作为高级覆盖，并持久化覆盖来源；
- Router 只能选择已注册的 public Agent 或 Team，不能授予写入、命令、网络、委派或更多预算；
- 明确任务先由确定性规则匹配，歧义任务才使用低成本结构化模型；低置信度、高风险能力差额和 Conversation 身份切换必须确认；
- 路由结果显示执行模式、Agent/Team、简短依据、置信度、能力需求和预算摘要，但不显示原始思维链；
- `inspect`、`resume`、`changes`、`agents`、`serve` 等管理操作仍是显式命令。

## 6. 阶段 10 Web 多 Agent 目标

Web 多 Agent 同样是明确开发目标，但不在 React 中重新实现路由或编排。阶段 10 复用阶段 9.3 的 TeamSpec 和阶段 9.4 的 Task Router、Run/child Run、审批、预算与安全展示语义：

- 主界面使用一个自然语言任务输入框并展示后端路由预览；Agent/Team 选择器只作为高级覆盖；
- 用节点流/纵向时间线展示父子 Run 和 Coordinator→Coder→Reviewer→Finalizer 阶段；
- 每个节点显示 Agent、Provider、模型、状态、耗时、Token、费用、租约摘要和产物；
- 子 Agent 等待审批时在统一审批中心处理，并能恢复父流程；
- 支持展开子 Run 事件、Diff、测试结果和审查结论；
- SSE 断线后按 sequence 恢复，同一后端展示投影在 CLI 和 Web 得到一致状态；
- 只展示安全工作动态，不展示原始模型思维链。

Web 的完成门禁包括单 Agent 和固定 Team 两条 E2E，覆盖子审批、失败、取消、重启恢复、预算耗尽和最终汇总。自主 Agent-as-Tool 产品入口延后到阶段 12 的真实需求扩展，不阻塞 Web MVP。
