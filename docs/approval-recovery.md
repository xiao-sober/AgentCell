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
