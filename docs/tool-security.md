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

Memory 和 Agent delegation 工具尚未实现。内置 coordinator 仍只选择三个只读工作区工具；生产工具必须由 AgentSpec 显式选择，并由 Run 的 CapabilityLease 再次授权。不存在“已经注册，所以默认可用”的隐式能力。

## 2. ToolRegistry 与 ToolPolicy

每个注册项必须同时提供：

- 稳定小写工具名；
- 非空说明；
- `extra="forbid"` 的 Pydantic 参数模型；
- 不可变 `ToolPolicy`；
- 异步 Handler。

Registry 拒绝重复注册和非严格参数 Schema，不允许后注册工具静默覆盖已有实现。

`ToolPolicy` 包含风险、审批、幂等性、超时、最大输出字节数和能力集合。`DANGEROUS` 工具在 Schema 层就必须声明需要审批；`FORBIDDEN` 工具无论是否传入审批标志都不会执行。

阶段 4 的执行器不自动重试任何工具。后续若增加工具重试，也只能由统一编排层对 `idempotent=True` 的工具执行，并同时更新预算和事件。

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

## 5. 工作区路径安全

所有工作区请求必须经过 `WorkspacePathResolver`：

1. 拒绝空路径、盘符路径、UNC、绝对路径和父目录穿越；
2. 拒绝 `.env*`、`.git`、SSH、云凭据目录、私钥等敏感路径；
3. 解析真实路径并确认仍位于工作区；
4. 解析租约范围并确认目标位于至少一个 `filesystem_read` 范围；
5. 检查目标类型是文件还是目录。

搜索不会跟随 symlink 或 Windows reparse point。读取显式 symlink 时会先解析最终目标，因此指向工作区外的链接或 junction 会被拒绝。

`workspace.read` 最多一次读取 64 KiB，并避免在多字节 UTF-8 字符中间返回 continuation offset。包含 NUL 或无法解码为 UTF-8 的内容按二进制文件拒绝。

`workspace.search` 只做字面量匹配，不接受正则表达式；文件数、单文件字节数、匹配数和预览长度都有上限。隐藏文件默认跳过，敏感文件始终跳过。

## 6. 输出与 Artifact

Handler 输出必须是 Pydantic 模型或合法 JSON 值。执行器使用 UTF-8 JSON 的实际字节数检查 `max_output_bytes`：

- 未超限：直接返回内容；
- 超限且存在 ArtifactStore：完整 JSON 保存为 Artifact，ToolResult 只返回引用并标记 `truncated=true`；
- 超限且没有 ArtifactStore：抛出 `ToolOutputTooLargeError`，不把超大内容放入事件或模型上下文。

阶段 4 定义 `ArtifactStore` 协议并验证转存路径；阶段 7 已提供文件 Artifact Store、数据库元数据、加载哈希校验和检查点引用恢复。生产工具仍必须通过该协议外置大输出。

阶段 7.1 已接入实际生产工具。写入、删除、Shell 和 HTTP 的参数、能力、审批、执行账本、Artifact 和恢复细节见 [生产工具安全边界](production-tools.md)。

## 7. 已知边界

- 当前是进程级路径约束，不是操作系统强沙箱；
- 路径解析和打开文件之间仍存在操作系统级 TOCTOU 窗口，不能把工作区交给同时具有敌意写权限的进程；
- Shell 和网络工具没有注册，因此阶段 4 不声称已提供命令沙箱或 HTTP SSRF 防护；
- 审批持久化、暂停恢复和 EventStore 绑定属于阶段 5 及后续阶段。

这些边界不应通过放宽默认租约规避。需要更强隔离时，应在后续 Sandbox 里程碑增加独立进程或操作系统级隔离。
