# 审批、检查点、恢复与回放

## 1. 阶段 6 边界

阶段 6 使用 PydanticAI 原生 deferred-tool 机制实现暂停恢复，不自行重写 Function Calling 协议。Guarded/Dangerous 工具在 ToolExecutor 完成参数与能力检查后、实际执行前产生审批；Run 进入 `waiting_approval`，不会把审批缺失记录为工具失败。

阶段 7.1 已让写入、删除、Shell 和 HTTP 生产工具复用本机制；它们没有单独的审批捷径。具体工具边界见 `docs/production-tools.md`。

## 2. 审批信息

每条审批持久化以下内容：

- Run、Agent、Provider、模型和 Provider Tool Call ID；
- 工具名称、严格结构化参数、风险等级和预计影响；
- 可选 Diff、完整 Diff ArtifactReference、剩余预算、幂等性和超时；
- pending/approved/rejected 状态、决定时间和可选说明；
- 修改参数后批准，以及仅当前 Run 有效的同类工具临时批准。

参数和检查点消息在落库前经过敏感字段递归脱敏。项目不提供永久全局批准。

## 3. 原子暂停

等待审批在一个数据库事务中完成：

1. 追加 `tool.approval_required`；
2. 追加 `checkpoint.created`；
3. 保存不可变 Checkpoint；
4. 保存 Run 的 `waiting_approval` 投影；
5. 追加 `run.status_changed`。

Checkpoint 保存 Agent、用户、原始任务、工作区、CapabilityLease、预算快照、PydanticAI 标准消息历史、待审批 ID、相关 Artifact UUID、父 Run 和当前 Run 临时授权。恢复不依赖原进程中的 Agent、模型或工具对象。

## 4. 决策与恢复

- 批准：使用 `ToolApproved` 恢复原 Provider Tool Call；
- 修改参数后批准：先使用工具 Pydantic Schema 重新校验，再通过 `override_args` 恢复；
- 拒绝：使用 `ToolDenied` 返回模型，允许 Agent 调整计划；
- 临时同类批准：只存入当前 Run 后续 Checkpoint，不扩展 CapabilityLease，也不跨 Run；
- 重复相同恢复请求：若 Run 已终止则返回当前投影，不重复执行；不同决定返回冲突。

决定和 `waiting_approval → running` 在外部工具执行前持久化。服务重启后可从最新 Checkpoint 恢复消息和预算。

### 4.1 阶段 9.2 权限模式

阶段 9.2 已为 CLI 增加 `request`、`auto`、`full` 三种权限模式。它们只决定已通过 AgentSpec、CapabilityLease、参数和安全策略校验的调用如何取得审批，不改变工具风险，也不扩大租约：

- `request`：GUARDED/DANGEROUS 都创建 pending 审批并等待用户；
- `auto`：PolicyEngine 可自动决定租约内 GUARDED，DANGEROUS 仍等待用户；
- `full`：PolicyEngine 可自动决定显式租约内 GUARDED/DANGEROUS；
- FORBIDDEN 和任何安全校验失败在所有模式下一律拒绝。

自动决定必须记录 `decision_source`（policy-auto 或 policy-full）、模式、规则版本、理由和时间，复用相同的 approved/rejected 事件、Checkpoint、预算更新和执行账本。模型不得成为审批主体；自动决定不得伪装为用户决定，也不得形成跨 Run 或永久全局批准。

### 4.2 阶段 9.2 文件变更恢复

审批恢复解决“批准前暂停后如何继续”，FileChange 恢复解决“文件已经替换但 SQLite 账本尚未完成时如何对账”。当前实现会在执行写入前持久化 before/after Artifact、before/预期 after 哈希和完整 Diff，随后使用 `prepared → applied → completed` 状态记录文件系统副作用。

重启恢复时：当前哈希等于预期 after 则补写完成；等于 before 才允许基于原批准继续；二者都不等则标记 conflict 并停止。非幂等执行账本仍负责防止盲目重复调用，FileChange 对账不得绕过它。

用户请求回滚时创建新的审批和反向 FileChange：修改/补丁恢复 before Artifact，新建文件转为受审批删除，删除文件转为受审批恢复。只有当前状态仍与原 after 状态一致时才能执行；回滚后保留原记录并建立 `reverts_change_id` 关联，不删除历史。Git 仅补充 HEAD/dirty/path Diff 信息，不参与自动 reset 或全工作区恢复。

## 5. 非幂等防重放

`tool_executions` 使用 `(run_id, provider_call_id)` 唯一键：

- 首次执行先持久化 `started`；
- 工具结果在 Run 继续前保存为 `completed`；
- 已完成调用直接返回原结果；
- 处于 started/failed 的非幂等调用一律拒绝再次执行，进入人工处理边界；
- 只有声明 `idempotent=True` 的调用允许重新认领。

这避免进程在“外部副作用已发生、Run 尚未继续”窗口崩溃后重复删除、发送或提交。

## 6. 回放与分支

ReplayService 按 sequence 连续读取只追加事件，可投影完整 Run 或指定前缀的状态。分支要求指定序号之前存在可恢复 Checkpoint；新 Run：

- 使用原 Run 作为 `parent_run_id`；
- 保存准确的 `source_run_id` 和 `source_sequence`；
- 以 `paused` 状态创建，等待后续显式恢复；
- 不复制来源序号之后的事件或状态。

CLI 当前提供：

```powershell
uv run agentcell replay <run-id>
uv run agentcell replay <run-id> --through-sequence 18
uv run agentcell branch <run-id> --from-sequence 18
uv run agentcell cancel <run-id>
```

CLI 直接调用业务服务，不请求本机 FastAPI。
