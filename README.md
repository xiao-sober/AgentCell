# AgentCell

AgentCell 是一个本地优先、事件溯源、能力隔离、模型可替换，并支持暂停恢复与多 Agent 协作的轻量级 Agent Runtime。

当前仓库已完成阶段 7.1：在阶段 7 的记忆与 Artifact 基础上，新增带 Diff/哈希并发保护的工作区写入、非幂等删除、argv 白名单 Shell，以及具备 DNS 固定和逐跳重定向复核的受限 HTTPS 工具。项目边界、技术约束和完成标准以 [AGENTS.md](AGENTS.md) 为准。

## 开发资料

- [开发步骤](docs/development-steps.md)：从工程基线到 M1–M4 的分阶段实施与验收顺序。
- [技术栈说明](docs/technology-stack.md)：前后端技术的职责、使用边界、接入阶段和官方资料。
- [Provider 工程](docs/provider-engineering.md)：模型配置、真实适配器、Fake Provider、错误重试、密钥与测试开关。
- [工具安全边界](docs/tool-security.md)：ToolPolicy、能力租约、事件预算顺序、工作区路径和 Artifact 规则。
- [审批与恢复](docs/approval-recovery.md)：审批数据、原子暂停、重启恢复、非幂等防重放、回放和分支。
- [记忆、上下文与 Artifact](docs/memory-context-artifacts.md)：四层记忆、FTS5 排序、Memory Policy、成对裁剪和 Artifact 恢复。
- [生产工具安全边界](docs/production-tools.md)：工作区写入/删除、Shell、HTTP、Diff 审批、SSRF 防护和当前限制。
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
```

CLI 不会自动修改数据库结构；首次运行或迁移变更后必须显式执行 Alembic。

生产依赖会随实际垂直切片引入；当前还已使用 Typer 和 Rich 提供命令行入口与结构化终端输出。
