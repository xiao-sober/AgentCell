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

真实契约同时包含非流式纯文本、流式纯文本和 Function Calling。Provider 的 Agent 运行失败但纯文本测试通过时，使用 `-k function_calling -vv` 验证工具定义、思考内容回传与第二轮模型调用；使用 `-k streaming -vv` 验证 SSE 与 Usage。离线 `test_adapters.py` 还会检查 `max_tokens`、并行工具开关、DeepSeek V4 请求中不出现 `tool_choice`，以及两种响应模式的缓存 Usage 映射。

`unit/test_tool_bridge.py` 覆盖点号领域工具名到 Provider 安全别名的映射、正常阶段预算指令稳定性、按预算比例预留最终输出尝试，以及 deferred approval/delegation 恢复仍保留工具的边界。

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

后续预算加固覆盖 Token 感知收尾、正常阶段预算提示稳定性、三次最终输出尝试、无效最终输出错误分类、默认 200k/40k/240k Token 上限、CLI 输入/总 Token 覆盖参数，以及消息数与估算 Token 双阈值的工具对安全上下文裁剪。

Run Usage 回归还覆盖 Provider 缓存读取/写入 Token 的模型包装层记账、DeepSeek 流式/非流式缓存字段映射、子树回卷，以及 CLI 普通完成行和 JSON 预算快照输出。

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

阶段 8 新增：

- `integration/test_agent_delegation.py`：Agent as Tool、父子 Run、预算回卷、只读 reviewer、子失败结构化传播、子审批后连续恢复父 Run，以及子执行中断和终态结算故障窗口；
- `integration/test_handoff.py`：Coordinator → Coder → Reviewer → Finalizer 的确定性顺序、委派关联、终态子结果恢复、取消收敛、事件与根终态；
- `unit/test_budgets.py`：子树真实 Usage 汇总且不重复累计墙钟时间；
- `integration/test_migrations.py`：revision `20260713_0004` 的委派表、索引及升降级。

阶段 9 新增：

- `unit/test_agui.py`：工具调用 ID 一致性、结果映射和复合 SSE 游标校验；
- `integration/test_api.py`：Problem Details、后台 Run、AG-UI/SSE 顺序与续传、Provider 脱敏、工具资源及 Agent 重启持久化；
- `integration/test_cli.py`：补充 inspect、agents/tools JSON 输出和错误退出码；
- `replay/test_replay.py`：分支检查点通过统一 RunService 恢复入口继续执行；
- `integration/test_migrations.py`：revision `20260713_0005` 的 Agent 定义表及升降级。

阶段 9.1 新增：

- `integration/test_conversations.py`：第二轮历史继承、跨进程继续、独立 Run、消息 sequence、思考内容剥离、用户隔离和并发回合冲突；
- `integration/test_api.py`：Conversation 创建、会话内新 Run、有序消息读取和 403 作用域拒绝；
- `integration/test_cli.py`：`agentcell chat` 创建会话并按 ID 跨进程继续；
- `integration/test_migrations.py`：revision `20260714_0006` 的 Conversation/消息表、索引及升降级。

阶段 9.2 已新增：

- CLI Agent 选择：`run/chat --agent coder`、Conversation 固定 Agent 冲突和 coordinator/reviewer 只读回归；
- 权限模式矩阵：request、auto、full 对 SAFE/GUARDED/DANGEROUS/FORBIDDEN 的决定，以及自动审批主体和审计数据；
- 租约参数：重复 `--allow-write`/`--allow-command`、Windows 路径规范化、敏感路径、工作区逃逸和未授权命令；
- Diff 审批与恢复：人工/自动决定均复用 Checkpoint 和执行账本，非幂等调用不重放；
- Rich 流式输出：文本、工具、审批、预算、子 Agent、终态、sequence 去重、Ctrl+C、EOF、TTY/非 TTY 和 `--no-stream`；
- 输出契约：每轮 Run/Conversation ID、退出续聊命令、JSON/NDJSON 不混入 ANSI，且不包含原始思维链或敏感参数。
- ChangeSet/FileChange：创建、修改、补丁、删除的 before/after Artifact、哈希、Diff、Run/审批/工具调用关联和重启查询；
- 存储额度：单文件/单 Run 逻辑字节上限在 Artifact 落盘和文件副作用前拒绝超限变更；
- 故障恢复：prepared/applied/completed/conflict 各窗口注入崩溃，按当前哈希确定补写、继续或冲突，非幂等操作不盲目重放；
- 安全回滚：修改恢复、新建删除、删除恢复、重复回滚、后续用户修改冲突、Artifact 缺失/篡改/超预算和审计历史不可改写；
- Git 可选增强当前覆盖：clean/dirty、untracked/ignored、无 HEAD、非 Git、Git 不可用和基础路径编码；嵌套仓库、worktree/submodule、HEAD/分支中途变化和复杂编码故障注入属于阶段 11 加固；
- Git 安全：只运行受控只读 argv，且自动路径绝不执行 reset/checkout/restore/clean/stash/commit/push。
