# Agent 与多 Agent 协作现状

本文说明 AgentCell 当前注册的 Agent、实际可用边界、模型来源，以及 CLI/Web 多 Agent 产品化目标。Agent 是无状态 `AgentSpec`；只有创建 Run 后才产生运行实例、预算、能力租约、事件和检查点。

## 1. 当前内置 Agent

| ID | 产品可见性目标 | 职责 | 工具与能力上限 | 步骤/委派上限 |
|---|---|---|---|---|
| `coordinator` | public | 分析、规划、委派和整合 | workspace list/read/search；应用以 collaborative 模式构建时增加 `agent.delegate` | `max_steps=20`；collaborative 时 `max_children=3`、`max_depth=2` |
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

展示的是应用本次启动后合并得到的有效 AgentRegistry。未传模型时，内置 Agent 使用 `agentcell.toml` 中第一个模型；当前示例配置第一个是 `qwen_plus`，所以六个内置 Agent 都显示 `qwen_plus`。这不表示 Run 不能使用 `--model-ref deepseek_pro`。

应用随后读取数据库中的持久化 AgentSpec：同 ID 记录替换内置定义，新 ID 记录追加。当前 Registry 只保存最终 AgentSpec，替换时丢失来源信息，因此 CLI 还不能区分：

- `builtin`：纯内置定义；
- `persisted`：数据库新增定义；
- `persisted_override`：数据库同 ID 定义覆盖内置 Agent；
- 本次列表使用的默认模型与某个历史 Run 的实际模型。

阶段 9.2.2 增加独立的注册来源和产品可见性元数据，不能把 `source` 塞进 AgentSpec。默认人类列表只展示 public Agent，`--all` 或 `--json` 可查看 internal 角色。目标人类输出为：

```text
ID           SOURCE               MODEL       ACCESS             DELEGATION
coder        builtin              qwen_plus   workspace-write    disabled
coordinator  builtin              qwen_plus   read-only          budget-disabled
reviewer     builtin              qwen_plus   read-only          unsupported
```

`--json` 应稳定返回 `source`、`visibility`、`declared_model_ref`、`effective_model_ref`、tools、capabilities、步骤/子 Agent 上限，以及当前默认 Run profile 下委派为何启用或禁用。默认表保持简洁，完整字段进入 `--verbose`/`--json`。`agents list --model-ref <ref>` 只用于预览该模型下的内置组合，不得改写持久化 Agent。

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

但当前普通 CLI 尚未产品化这些能力：默认 Run Budget 为 `max_children=0/max_depth=0`，CLI 没有子 Agent 预算入口；程序化 Handoff 也没有接入 `agentcell run/chat`。所以“后端存在”不等于“CLI 会自动调用”。Coder、Reviewer、Researcher、Summarizer 和 Finalizer 均没有 `agent.delegate`，不会自行调用其他 Agent。

## 4. 阶段 9.3 CLI 多 Agent 目标

CLI 多 Agent 是明确开发目标，并在阶段 10 Web 之前完成最小可用入口。首个产品入口只开放确定性 `software` Team，不同时开放模型自主委派：

```powershell
# 固定、可预测的软件交付流水线
uv run agentcell run --team software --model-ref deepseek_pro --approval-mode request "修复测试并独立审查"
```

- `--team software` 使用配置化的 Coordinator→Coder→Reviewer→Finalizer Handoff；它与显式 `--agent` 互斥，阶段、子预算、模型和租约来自版本化 TeamSpec；
- Agent-as-Tool 后端继续保留和测试，但自主 `--collaborate` 不作为阶段 10 门禁；只有确定性 Team 的成本、审批和恢复边界稳定后才产品化；
- 子 Agent 的 CapabilityLease 必须是父租约子集，预算从父剩余量预留；子审批、取消、失败、恢复和重放必须正确传播；
- Live CLI 展示父/子 Agent、当前阶段、模型、子预算和审批状态，完成行输出 root Run ID、child Run IDs 和 Conversation ID；
- 不提供“任意无限子 Agent”或隐式跨 Provider 降级。不同 Agent 可显式使用不同模型，但每次选择都必须持久化并有事件记录。

## 5. 阶段 10 Web 多 Agent 目标

Web 多 Agent 同样是明确开发目标，但不在 React 中重新实现编排。阶段 10 复用阶段 9.3 已稳定的 TeamSpec、Run/child Run、Handoff、审批、预算和安全展示语义：

- 创建任务时选择单 Agent 或 `software` Team；
- 用节点流/纵向时间线展示父子 Run 和 Coordinator→Coder→Reviewer→Finalizer 阶段；
- 每个节点显示 Agent、Provider、模型、状态、耗时、Token、费用、租约摘要和产物；
- 子 Agent 等待审批时在统一审批中心处理，并能恢复父流程；
- 支持展开子 Run 事件、Diff、测试结果和审查结论；
- SSE 断线后按 sequence 恢复，同一后端展示投影在 CLI 和 Web 得到一致状态；
- 只展示安全工作动态，不展示原始模型思维链。

Web 的完成门禁包括单 Agent 和固定 Team 两条 E2E，覆盖子审批、失败、取消、重启恢复、预算耗尽和最终汇总。自主 Agent-as-Tool 产品入口延后到阶段 12 的真实需求扩展，不阻塞 Web MVP。
