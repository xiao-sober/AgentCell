# 阶段 7.1：生产工具安全边界

本文说明 `workspace.write/patch/delete`、`shell.run/test` 和 `http.request` 的实际安全边界。所有工具都只能通过：

```text
ToolRegistry
  → ToolExecutor
  → CapabilityLease
  → Approval
  → ToolExecutionLedger
  → Artifact Store
  → Checkpoint
```

工具实现不能直接修改 Run 状态，也不能绕过预算、事件、超时或恢复账本。

## 1. 工作区写入工具

### `workspace.write`

- 新建 UTF-8 文件，或完整替换现有 UTF-8 文件；
- 新文件不得提供 `expected_sha256`；
- 替换现有文件必须提供最近一次 `workspace.read` 返回的 `sha256`；
- 单次内容最大 1 MiB；
- 写入使用同目录临时文件、刷盘和原子替换；新文件使用不覆盖目标的原子链接发布。

### `workspace.patch`

- 使用 `old_text`、`new_text` 和 `expected_replacements` 做结构化精确替换；
- 必须提供完整文件的 `expected_sha256`；
- 哈希、匹配次数或目标类型变化时明确失败；
- 输入和结果文件最大 4 MiB。

### `workspace.delete`

- 只删除普通文件，不删除目录；
- 标记为 DANGEROUS、需要审批且非幂等；
- 必须提供 `expected_sha256`，真正删除前再次复核；
- 恢复账本不会重新执行已开始且结果不明确的删除。

三种工具都要求目标同时落在 `filesystem_read` 和 `filesystem_write` 租约中，拒绝绝对路径、父目录穿越、敏感文件、symlink、Windows junction/reparse point 和租约外路径。

## 2. Diff 与审批

审批前通过只读 Preview 计算实际 unified Diff，不执行目标变更。审批展示：Agent、Provider、模型、参数、风险、影响、剩余预算、幂等性、超时和 Diff。

- 小 Diff 直接进入审批；
- 超过 30 KiB 的完整 Diff 保存到 Artifact；
- 审批只内联有界片段和 ArtifactReference；
- Diff Artifact UUID 同时写入检查点，服务重启后仍可加载；
- 超过 32 KiB 的工具参数不会进入领域事件，事件只保留大小摘要，完整恢复参数保存在审批/检查点记录中。

批准后若文件哈希已经变化，操作以 `workspace_state_conflict` 失败，绝不按旧 Diff 覆盖新内容。

## 3. Shell 工具

`shell.run` 和 `shell.test` 使用同一保守执行边界：

- 默认不可用；Run 必须同时授予 `shell.execute`、对应读写工作区和命令白名单；
- `command` 只接受不含路径和参数的可执行名称，并必须精确匹配租约；
- 可执行文件从去除相对项后的 PATH 中预解析，再以绝对路径启动；
- 参数以数组传给 `asyncio.create_subprocess_exec`，从不使用 `shell=True`；
- cwd 必须是读租约内的工作区目录；
- 子进程只获得 PATH、PATHEXT、SYSTEMROOT、WINDIR、TEMP/TMP、VIRTUAL_ENV 和 Python UTF-8 设置，不继承 API Key 或完整环境变量；
- 总捕获输出默认 1 MiB、最大 4 MiB；超限立即终止进程；
- ToolPolicy 超时为 120 秒，取消或失败时终止当前子进程；
- 超过 64 KiB 的成功结果由 ToolExecutor 转为 Artifact。

两个 Shell 工具都标记为 DANGEROUS、需要审批且非幂等。`shell.test` 只是产品语义名称，不能假定任意测试命令可安全自动重试。

首版 Shell 是进程级约束，不是强隔离沙箱。被批准的解释器或构建工具仍可能自行访问系统资源，也可能创建后代进程；高对抗场景需要后续强隔离 Sandbox。

## 4. HTTP 工具

`http.request` 当前采用严格边界：

- 只允许 HTTPS 443；
- Host 必须匹配 `network_domains` 租约的精确域名或子域名；
- 禁止 URL 用户名/密码和 token、secret、password、signature 等敏感 query 参数；
- 禁止 Authorization、Cookie、Proxy-Authorization、X-API-Key、Host、Connection 和 Transfer-Encoding 等危险请求头；
- 每一跳都解析全部 DNS 地址，任一地址非公网即拒绝；
- 实际请求 URL 固定为已验证 IP，同时保留原 Host 和 TLS SNI，避免第二次解析造成 DNS rebinding；
- 若传输层提供真实 peer 地址，再复核 peer 必须属于固定 DNS 结果；
- 重定向最多五跳，每一跳重新执行协议、域名、DNS、端口和敏感参数检查；
- 不读取系统代理配置，避免通过环境代理绕过地址限制；
- 请求体最大 1 MiB，响应默认 1 MiB、最大 4 MiB；超限中止读取；
- 二进制响应使用 Base64 表达，较大结果由 ToolExecutor 外置为 Artifact；
- 只返回有限安全响应头。

当前单一 `http.request` 对所有方法都要求审批，并标记为非幂等；这比“仅非只读方法审批”更严格，也确保 POST/PUT/PATCH/DELETE 不会自动重试。

## 5. 注册与使用

`register_workspace_tools` 注册六个工作区工具；`register_shell_tools` 和 `register_http_tools` 必须显式调用。仅注册工具不会授予能力：AgentSpec 必须选择工具，RunRequest 还必须提供相应 CapabilityLease，两层缺一都会默认拒绝。

内置 coordinator 继续保持只读，避免 CLI 在没有完整交互式审批中心时默认扩大权限。阶段 7.1 的生产工具可由 RunService 使用自定义 AgentSpec 显式启用；完整 CLI/API 管理入口在产品接口阶段提供。

## 6. 当前非目标

- 不宣称 Shell 具备容器或操作系统级强隔离；
- 不允许任意端口、HTTP 明文、本机服务、私网或云元数据访问；
- 不支持任意认证请求头或在 URL 中传递密钥；
- 不实现目录递归删除；
- 不自动重试任何写入、删除、Shell 或 HTTP 请求。
