# AgentCell

AgentCell 是一个本地优先、事件溯源、能力隔离、模型可替换，并支持暂停恢复与多 Agent 协作的轻量级 Agent Runtime。

当前仓库已完成阶段 3：在 Run 生命周期、事件、预算和 SQLite EventStore 基础上，新增了统一 ModelSpec、ProviderFactory、百炼与 DeepSeek 适配器、HTTPX Client 生命周期、分类错误及确定性 Fake Provider；尚未提供完整 RunService 或可运行 CLI。项目边界、技术约束和完成标准以 [AGENTS.md](AGENTS.md) 为准。

## 开发资料

- [开发步骤](docs/development-steps.md)：从工程基线到 M1–M4 的分阶段实施与验收顺序。
- [技术栈说明](docs/technology-stack.md)：前后端技术的职责、使用边界、接入阶段和官方资料。
- [Provider 工程](docs/provider-engineering.md)：模型配置、真实适配器、Fake Provider、错误重试、密钥与测试开关。
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

生产依赖会随实际垂直切片引入；当前已使用 Pydantic v2、pydantic-settings、PydanticAI slim、HTTPX、SQLAlchemy 2、Alembic 和 aiosqlite。
