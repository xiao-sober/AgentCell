# 测试目录

测试按行为边界分层：

- `unit/`：生命周期、预算、租约、路径、裁剪、评分和参数映射；
- `integration/`：Run、工具、审批、恢复、SQLite、SSE 和 CLI；
- `provider_contract/`：Fake、百炼和 DeepSeek 的统一 Provider 契约；
- `replay/`：事件回放、检查点恢复和分支确定性；
- `e2e/`：用户创建、审批、取消、恢复、时间线和记忆路径。

真实 Provider 测试必须由环境变量显式启用，默认测试不得访问线上模型或消耗额度。

阶段 3 的离线 Provider 契约测试会把 PydanticAI 的线上请求总开关设为关闭。真实百炼或 DeepSeek 冒烟测试只有同时满足以下条件才运行：

```powershell
$env:AGENTCELL_RUN_LIVE_PROVIDER_TESTS="1"
$env:DASHSCOPE_API_KEY="..."  # 或 DEEPSEEK_API_KEY
uv run pytest tests/provider_contract/test_live_providers.py
```

仅设置 API Key 不会触发付费测试。

阶段 4 新增：

- `unit/test_policy.py`：默认拒绝、作用域规范化和父子租约不可扩权；
- `unit/test_tool_executor.py`：Registry、参数、能力、预算、事件顺序、超时、异常脱敏和 Artifact 转存；
- `integration/test_workspace_tools.py`：临时工作区中的盘符、UNC、穿越、敏感文件、租约、symlink/junction、UTF-8 分块和有限搜索。

工作区安全测试不读取真实项目密钥或用户目录，所有文件和 Windows junction 都在 pytest 临时目录中创建。

阶段 5 新增：

- `unit/test_agents.py`：AgentSpec 不变量、Registry 顺序、重复注册和未知 ID；
- `integration/test_run_service.py`：文本、单次/多次只读工具、模型/工具失败、预算超限、取消、事件顺序和终态；
- `integration/test_cli.py`：显式离线 Fake、JSON 输出、SQLite 持久化和 CLI 到 RunService 的直接入口。

CLI 集成测试只使用 Fake Provider 和临时迁移数据库，不访问网络，也不修改仓库的 `.agentcell` 数据库。

阶段 6 新增：

- `unit/test_approvals.py`：决定组合约束和敏感参数脱敏；
- `integration/test_approval_recovery.py`：审批暂停、服务重启、批准/拒绝/修改、临时授权和重复恢复；
- `integration/test_tool_execution_ledger.py`：非幂等 started 调用拒绝重放和 completed 结果复用；
- `replay/test_replay.py`：完整/前缀回放一致性和指定 sequence 分支来源边界。

阶段 7 新增：

- `integration/test_memory_service.py`：四层记忆 CRUD、去重、过期、敏感策略、Semantic 批准、FTS5 排序与作用域隔离；
- `integration/test_context_management.py`：工具调用/结果成对裁剪、记忆注入、专用 Fake 摘要模型、运行时召回事件和大输出外置；
- `integration/test_artifact_store.py`：Artifact 去重、大小边界、篡改检测、检查点引用及进程重启后恢复加载；
- `integration/test_migrations.py`：revision `20260713_0003` 的普通表、FTS5 虚拟表、同步触发器及升降级。

阶段 7.1 新增：

- `integration/test_production_tools.py`：工作区创建/补丁/删除、Diff Artifact、审批重启、哈希冲突、Shell argv/环境/输出和 HTTP DNS 固定/重定向/SSRF/响应限制；
- `integration/test_workspace_tools.py`：在既有读取逃逸测试上增加写入穿越 symlink 或 Windows junction 的拒绝验证；
- 复用 `integration/test_approval_recovery.py` 与 `integration/test_tool_execution_ledger.py` 验证审批决定和非幂等防重放。
