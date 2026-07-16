# AgentCell

AgentCell 是一个本地优先、事件溯源、能力隔离、模型可替换，并支持暂停恢复与多 Agent 协作的轻量级 Agent Runtime。

当前仓库已完成阶段 9.2、9.2.1、9.2.2、9.3 和 9.4。`agentcell run/chat` 默认使用统一执行入口；auto chat 中的问候、身份/能力询问和短一般知识问题直接进入无工具、无委派的 `assistant` Run，项目分析、修改、审查、研究和交付任务继续使用 `TaskRoutingService`。显式 `--agent`/`--team` 仍作为受 Registry、Lease 与 Budget 约束的高级覆盖。明确任务意图走可复现规则，歧义任务使用有请求、Token、输出和超时上限的 PydanticAI 结构化分类。权威任务记录路由 Usage、决定、child、审批、检查点和终态；single-Agent child 的公开文本以及 single-Agent/Team child 的工具生命周期会安全、实时地投影到 root，动态框显示 Agent、工具名、目标路径或有界命令摘要，但不复制工具输出内容。`fixed/auto` Conversation 始终保持原对话模型绑定。项目边界、技术约束和完成标准以 [AGENTS.md](AGENTS.md) 为准。

## 开发资料

- [开发步骤](docs/development-steps.md)：从工程基线到 M1–M4 的分阶段实施与验收顺序。
- [技术栈说明](docs/technology-stack.md)：前后端技术的职责、使用边界、接入阶段和官方资料。
- [Provider 工程](docs/provider-engineering.md)：模型配置、真实适配器、Fake Provider、错误重试、密钥与测试开关。
- [工具安全边界](docs/tool-security.md)：ToolPolicy、能力租约、事件预算顺序、工作区路径和 Artifact 规则。
- [审批与恢复](docs/approval-recovery.md)：审批数据、原子暂停、重启恢复、非幂等防重放、回放和分支。
- [记忆、上下文与 Artifact](docs/memory-context-artifacts.md)：四层记忆、FTS5 排序、Memory Policy、成对裁剪和 Artifact 恢复。
- [生产工具安全边界](docs/production-tools.md)：工作区写入/删除、Shell、HTTP、Diff 审批、SSRF 防护和当前限制。
- [API、AG-UI/SSE 与 CLI](docs/api-cli.md)：HTTP 资源、错误语义、复合事件游标、启动方式和命令清单。
- [Agent 与多 Agent 协作](docs/agents.md)：当前内置 Agent、注册来源、单/多 Agent 实际边界及 CLI/Web 产品化目标。
- [项目交接](docs/handoff.md)：当前状态、关键决策、环境限制和下一步入口。

## 当前结构

```text
src/agentcell/   Python 领域与应用包边界
tests/           单元、集成、Provider 契约、回放与 E2E 测试
migrations/      Alembic 迁移工作区
web/             React 工作台预留工作区
docs/            开发计划与交接文档
```

## 环境要求

- Python 3.12+
- uv
- Node.js 及 pnpm（进入 Web 阶段后使用）

初始化本地数据库：

```powershell
uv run alembic upgrade head
```

执行确定性的离线 Run：

```powershell
uv run agentcell run --offline-fake "分析当前项目"
uv run agentcell run --offline-fake --json "分析当前项目"
uv run agentcell run --team software --offline-fake --approval-mode request "修复测试并独立审查"
uv run agentcell chat --offline-fake --workspace .
uv run agentcell chat --conversation-id <conversation-id> --offline-fake
uv run agentcell run --model-ref qwen_plus --max-requests 20 --max-tool-calls 40 --max-input-tokens 300000 --max-total-tokens 360000 "分析当前项目"
uv run agentcell serve --offline-fake
```

CLI 不会自动修改数据库结构；首次运行或迁移变更后必须显式执行 Alembic。

Run 默认累计预算为 200,000 输入 Token、40,000 输出 Token 和 240,000 总 Token。CLI 可按单次任务提高请求、工具、输入 Token 和总 Token 上限；运行时使用正常阶段内稳定的预算提示，并综合剩余请求、工具与 Token 提前关闭工具，在预算允许时为最终回答保留最多三次输出尝试。

Run 完成后，CLI 会同时显示请求数、工具调用数、输入/输出/总 Token、缓存读取/写入 Token 和缓存命中率；`--json` 输出在 `budget.used` 中保留相同原始计数。

`agentcell run` 保持无隐式历史继承的单任务入口。`agentcell chat` 的每次输入会在同一 Conversation 下创建新 Run，加载经过工具对安全裁剪、Token 限制、脱敏和 Artifact 压缩的已完成历史；新回合不会继承上一轮剩余预算、临时审批或扩权租约。普通问答使用最多 4 次模型请求、0 次工具、0 个子 Agent 的收窄预算。工作区任务会把 child 当前 Usage 增量折算为 root Budget 快照，因此请求、工具、Token 和缓存统计会在阶段执行中更新；Rich 回答区使用有界滚动尾窗持续展示最新增量，结束后再保留完整回答。

阶段 9.2 核心能力已经实现：`run/chat --agent coder`、显式写路径与命令租约、`request/auto/full` 三种权限模式、交互式 Diff 审批、每轮 Conversation ID、退出续聊命令，以及按持久化事件 sequence 去重的 Rich 流式文本/工具/预算状态。文件修改同时写入有单文件/单 Run 存储额度的 AgentCell ChangeSet/FileChange 账本、before/after Artifact、完整 Diff 和可选 Git HEAD/dirty/path Diff；`agentcell changes` 与对应 API 可在 Git 或非 Git 工作区查询并执行哈希保护的显式反向变更。系统不会自动调用 reset、checkout、clean、stash、commit 或 push，默认 coordinator 和 reviewer 继续只读。

阶段 9.3 的 `software` Team 只由显式 `--team` 启动，和 `--agent` 互斥；它不会开启自主委派，也不会扩大 CLI profile 生成的 root CapabilityLease。Team 默认 root 预算为 24 次模型请求和 48 次工具调用，按阶段职责分别分配；显式预算参数仍可覆盖。要让 Coder 执行测试，仍需显式添加 `--command-profile pytest` 或精确的 `--command`。

生产依赖会随实际垂直切片引入；当前已使用 FastAPI、Uvicorn、`ag-ui-protocol`、Typer 和 Rich 提供 HTTP、流式与命令行产品接口。
