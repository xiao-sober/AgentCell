# AgentCell 工具与能力安全边界

## 1. 阶段 4 实现范围

阶段 4 建立了工具从模型提议到实际执行之间的统一安全路径：

```text
ToolCall
  → Registry 查找
  → Pydantic 参数校验
  → ToolPolicy / CapabilityLease 授权
  → 必要时审批暂停与检查点
  → 工具预算预留
  → 超时保护下执行
  → JSON 输出校验与字节上限
  → ToolResult / ArtifactReference
```

当前已有实现并可显式注册的生产工具包括：

- `workspace.list`：列出一个获准目录的有限元数据；
- `workspace.read`：按 UTF-8 完整字符边界分块读取文件；
- `workspace.search`：在有限文件数、文件大小和结果数内执行字面量搜索。
- `workspace.write/patch/delete`：带 Diff、哈希状态保护和审批的文件变更；
- `shell.run/test`：命令租约、argv、最小环境和输出限制；
- `http.request`：HTTPS 域名租约、DNS 固定、逐跳重定向和响应限制。

Memory 工具尚未实现。Agent delegation 已由阶段 8 实现，但默认 coordinator 仍保持三个只读工作区工具；只有显式使用 collaborative coordinator 或自定义 AgentSpec 才暴露 `agent.delegate`，并且 Run 的 CapabilityLease 必须再次授权。不存在“已经注册，所以默认可用”的隐式能力。

## 2. ToolRegistry 与 ToolPolicy

每个注册项必须同时提供：

- 稳定小写工具名；
- 非空说明；
- `extra="forbid"` 的 Pydantic 参数模型；
- 不可变 `ToolPolicy`；
- 异步 Handler。

Registry 拒绝重复注册和非严格参数 Schema，不允许后注册工具静默覆盖已有实现。

领域工具名允许使用点号表达命名空间，例如 `workspace.list`。Provider 请求边界会将其映射成 `workspace_list` 等可移植函数名；模型返回后必须解析回原名称，禁止把 Provider 别名写入审批、事件或执行账本。

`ToolPolicy` 包含风险、审批、幂等性、超时、最大输出字节数和能力集合。`DANGEROUS` 工具在 Schema 层就必须声明需要审批；`FORBIDDEN` 工具无论是否传入审批标志都不会执行。

执行器不自动重放工具。对发生在副作用和工具预算预留之前、且明确标记为模型可纠正的安全错误，运行时允许全 Run 共享的一次 `ModelRetry`：当前包括安全相对路径的租约不匹配，以及把目录传给文件工具或把文件传给目录工具。纠正请求计入模型请求预算；原错误记录 `tool.failed`，但不消耗工具调用预算。第二次可纠正错误直接失败。路径逃逸、敏感路径、绝对路径、未授权命令和其他安全拒绝不进入该纠正路径。

## 3. CapabilityLease

租约默认没有任何权限，支持的粗粒度能力为：

```text
filesystem.read
filesystem.write
network.request
shell.execute
agent.delegate
```

具体范围使用工作区相对路径、HTTPS 域名白名单和无参数的可执行文件名表达。路径不接受绝对路径或 `..`；网络范围不接受 scheme、端口、路径、本机、私网、链路本地、保留地址或云元数据地址；命令范围不接受拼接参数。

子租约必须逐项缩小父租约：

- 文件范围只能位于父范围内部；
- 网络域名只能相同或为父域名的子域；
- 命令必须是父命令集合的子集；
- 子 Agent 的剩余委派深度至少减少一层。

任何扩大都会抛出 `CapabilityEscalationError`。

## 4. 执行事件和预算顺序

执行器接收 Run 绑定的 `ToolEventSink` 和 `BudgetTracker`，自身不访问数据库，也不分配 Run event sequence。正常执行顺序固定为：

```text
tool.proposed
budget.updated
tool.started
tool.completed
```

参数错误、缺少能力、缺少审批或工具不存在时不会预留工具预算：

```text
tool.proposed
tool.failed
```

工具已经启动后发生超时、输出超限或实现错误，预算预留会保留，并以 `tool.failed` 结束。未知实现异常会转换为不包含原始异常正文的 `ToolExecutionError`。提议事件中的 password、API Key、Authorization 等字段在进入 Sink 前递归脱敏。

阶段 5 的 RunService 负责把这个 Sink 绑定到 EventStore，并把 `ToolApprovalRequiredError` 转换为审批请求、Run 暂停和检查点；阶段 4 不伪造尚不存在的审批持久化。

Run 进入有界收尾窗口时，会从后续模型请求中移除新工具并注入明确的最终回答指令。请求窗口至少覆盖一次初始最终输出和两次校验重试：例如 5 请求阶段在剩余 3 次时进入收尾，而不是等到最后一次才隐藏工具。工具预算恰好耗尽时也必须直接收尾。模型在无工具窗口中输出的无效工具协议只进入最终输出校验，不计作真实工具执行；AgentCell 的 BudgetTracker 仍在每次实际执行前实施硬上限。审批或委派恢复只在恢复起始请求处理 deferred tool result 时保留工具；首个新模型请求完成后，该例外立即失效。对保守识别出的纯测试修复任务，只有带结构化证据的真实成功 `shell.test` 才会设置持久化阶段策略并在同一执行中关闭后续工具；collect-only、未知输出和包含新增功能等意图的任务不启用此快路径。

一个 Provider 响应可能携带多个工具调用，且响应生成时看到的剩余额度可能小于调用数量。工具按确定性顺序逐个预留：额度内调用正常执行；第一个及后续超额调用都不执行，记录可审计的 `tool.failed / budget_exceeded` 并向模型返回结构化拒绝结果，然后强制进入无工具收尾。实际 `Usage.tool_calls` 永远不超过硬上限；这不是静默截断，也不把模型的批次尺寸误当成已执行副作用。

## 5. 工作区路径安全

所有工作区请求必须经过 `WorkspacePathResolver`：

1. 拒绝空路径、盘符路径、UNC、绝对路径和父目录穿越；
2. 拒绝 `.env*`、`.git`、SSH、云凭据目录、私钥等敏感路径；
3. 解析真实路径并确认仍位于工作区；
4. 解析租约范围并确认目标位于至少一个 `filesystem_read` 范围；
5. 检查目标类型是文件还是目录。

搜索不会跟随 symlink 或 Windows reparse point。读取显式 symlink 时会先解析最终目标，因此指向工作区外的链接或 junction 会被拒绝。

`workspace.read` 最多一次读取 64 KiB，并避免在多字节 UTF-8 字符中间返回 continuation offset。其 Schema 明确只接受文件；目录应交给 `workspace.list`。若模型误把目录传给 `workspace.read`，无副作用 preflight 会返回一次可纠正错误，而不是立即终止整个 Run。包含 NUL 或无法解码为 UTF-8 的内容仍按二进制文件拒绝。

`workspace.search` 只做字面量匹配，不接受正则表达式；文件数、单文件字节数、匹配数和预览长度都有上限。隐藏文件默认跳过，敏感文件始终跳过。

## 6. 输出与 Artifact

Handler 输出必须是 Pydantic 模型或合法 JSON 值。执行器使用 UTF-8 JSON 的实际字节数检查 `max_output_bytes`：

- 未超限：直接返回内容；
- 超限且存在 ArtifactStore：完整 JSON 保存为 Artifact，ToolResult 只返回引用并标记 `truncated=true`；
- 超限且没有 ArtifactStore：抛出 `ToolOutputTooLargeError`，不把超大内容放入事件或模型上下文。

阶段 4 定义 `ArtifactStore` 协议并验证转存路径；阶段 7 已提供文件 Artifact Store、数据库元数据、加载哈希校验和检查点引用恢复。生产工具仍必须通过该协议外置大输出。

阶段 7.1 已接入实际生产工具。写入、删除、Shell 和 HTTP 的参数、能力、审批、执行账本、Artifact 和恢复细节见 [生产工具安全边界](production-tools.md)。

阶段 9.2.2 通过唯一 CliRunProfile 暴露 AgentSpec、CapabilityLease 和审批模式：`--agent` 选择角色上限，`--write-scope`、`--command-profile`/`--command`、`--network-domain` 构建本次 Run 的最小租约，`--approval-mode` 只决定租约内操作由人工还是 PolicyEngine 审批。旧 `--allow-write`、`--allow-command`、`--permission-mode` 隐藏兼容一个版本且不得改变 Lease。最终工具集始终是 AgentSpec 工具与 Run 租约的交集；默认 coordinator 和 reviewer 继续只读，普通 coordinator Run 不再默认委派。

同阶段的 ChangeService 必须在写入前保存可验证 before Artifact、预期 after 哈希和 Diff，写入后保存 after Artifact，并通过状态机对账文件系统/SQLite 崩溃窗口。GitWorkspaceInspector 只能运行有界只读 Git argv 并输出工作区内路径；`.git` 仍是普通 workspace 工具的禁止路径。安全回滚只反转选中的 FileChange，当前哈希不匹配即冲突，不能借 Git 命令重置整个工作树。

## 7. 已知边界

- 当前是进程级路径约束，不是操作系统强沙箱；
- 路径解析和打开文件之间仍存在操作系统级 TOCTOU 窗口，不能把工作区交给同时具有敌意写权限的进程；
- Shell 和网络工具没有注册，因此阶段 4 不声称已提供命令沙箱或 HTTP SSRF 防护；
- 审批持久化、暂停恢复和 EventStore 绑定属于阶段 5 及后续阶段。

这些边界不应通过放宽默认租约规避。需要更强隔离时，应在后续 Sandbox 里程碑增加独立进程或操作系统级隔离。
