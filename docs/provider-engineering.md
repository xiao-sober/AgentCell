# AgentCell Provider 工程

## 1. 实现范围

阶段 3 已建立统一模型边界，并接入以下实现：

- 阿里云百炼（Alibaba Cloud Model Studio）`qwen3.7-plus`；
- DeepSeek 官方平台 `deepseek-v4-pro`；
- 完全离线、可脚本化的 Fake Provider；
- 可注入的 HTTPX AsyncClient、逐模型超时、代理环境变量和连接池；
- 统一 Usage、模型输出事件、错误分类和重试判断；
- 默认禁网、显式付费开关控制的 Provider 契约测试。

阶段 3 只负责稳定 Provider 边界，不实现 RunService、Agent Registry、工具执行、审批或 CLI 运行闭环。

## 2. 依赖与调用方向

```mermaid
flowchart LR
    TOML["agentcell.toml"] --> SETTINGS["AgentCellSettings / ModelSpec"]
    REF["model_ref"] --> FACTORY["ProviderFactory"]
    SETTINGS --> FACTORY
    SECRET["环境变量 / SecretResolver"] --> FACTORY
    FACTORY --> ADAPTER["注册的 ProviderAdapter"]
    CLIENT["可注入 HTTPX AsyncClient"] --> ADAPTER
    ADAPTER --> MODEL["PydanticAI Model"]
    MODEL --> LOOP["后续 Agent / RunService"]
```

调用方只提交稳定的 `model_ref`。`ProviderFactory` 从配置中取得 `ModelSpec`，通过注册表选择适配器；Agent、Tool、API 和 CLI 不需要也不允许判断厂商名称。

`ProviderAdapter` 只构造 PydanticAI 的 `Model`。AgentCell 不重复实现 OpenAI 兼容协议、Function Calling 或底层流式解析。

## 3. 配置模型

公共字段由 `ModelSpec` 定义：模型名、最大输出 Token、温度、超时、最大重试次数和 HTTP 连接池。联网 Provider 还必须给出 `api_key_env`，这里只保存环境变量名，不保存密钥值。

厂商专属字段由专属 Schema 和适配器解释：

| Provider | 专属字段 | 请求映射 |
| --- | --- | --- |
| 百炼 | `thinking`、`thinking_budget`、区域 `base_url` | OpenAI-compatible `extra_body.enable_thinking` 与 `extra_body.thinking_budget` |
| DeepSeek | `thinking`、`reasoning_effort` | `extra_body.thinking.type` 与 `reasoning_effort` |
| Fake | 注册的 `FakeScript` | 不读取密钥、不创建 HTTP Client、不访问网络 |

示例：

```toml
[models.qwen_plus]
provider = "bailian"
model = "qwen3.7-plus"
api_key_env = "DASHSCOPE_API_KEY"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
thinking = true
thinking_budget = 12000
max_output_tokens = 16000
timeout_seconds = 120
max_retries = 3

[models.deepseek_pro]
provider = "deepseek"
model = "deepseek-v4-pro"
api_key_env = "DEEPSEEK_API_KEY"
thinking = true
reasoning_effort = "high"
max_output_tokens = 16000
timeout_seconds = 120
max_retries = 3
```

百炼 API Key 具有区域属性，部署到其他区域时必须把 `base_url` 改成对应区域的官方端点。DeepSeek 适配器固定使用 PydanticAI `DeepSeekProvider` 的官方 API 端点，不提供静默代理或厂商降级。

## 4. 模型设置依据

本实现于 2026-07-10 对照当时官方资料确认：

- PydanticAI 的 `OpenAIChatModel` 可搭配 `AlibabaProvider`、`DeepSeekProvider` 和自定义 HTTPX Client；
- 百炼 OpenAI-compatible Chat API 用 `extra_body` 传递 `enable_thinking` 和 `thinking_budget`；
- `qwen3.7-plus` 支持混合思考模式；
- DeepSeek V4 用 `thinking.type` 开关思考模式，只接受 `high` 或 `max` 作为原生推理强度；
- 两家适配器按官方 Chat Completion 字段发送 `max_tokens`；百炼显式关闭并行工具调用，避免只读项目检查一次生成过大的工具批次；DeepSeek 不发送未在官方工具示例中使用的可选并行参数；
- DeepSeek V4 思考模式可接收工具定义，但会拒绝 `tool_choice`；专用 Chat Model 只在 DeepSeek 适配器内省略该字段，并保留 PydanticAI 对后续轮次 `reasoning_content` 的回传；
- AgentCell 的领域工具名继续使用 `workspace.list` 这类稳定名称；发送给模型前统一映射为 `workspace_list` 形式的可移植别名，以满足 DeepSeek 对函数名只能包含字母、数字、下划线和短横线的限制。工具执行、事件、审批和检查点仍保存原领域名称；
- DeepSeek 思考模式不使用 temperature、top_p、presence_penalty 或 frequency_penalty，本项目在配置期拒绝思考模式下的 temperature；
- 两家 Provider 的 Usage 统一通过 PydanticAI `RequestUsage` / `RunUsage` 转换，不在 Agent 代码中解析厂商响应字段。DeepSeek 的 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 是 Chat Completion Usage 顶层扩展字段：专用适配器在非流式响应和流式最终 usage-only chunk 中统一把命中量映射为 `cache_read_tokens`，同时在 details 中保留两项原始计数；缓存未命中量不等同于可观测的缓存写入量，因此不会写入 `cache_write_tokens`。

官方资料：

- [PydanticAI OpenAI-compatible models](https://pydantic.dev/docs/ai/models/openai/)
- [PydanticAI model testing](https://pydantic.dev/docs/ai/guides/testing/)
- [阿里云百炼深度思考](https://www.alibabacloud.com/help/en/model-studio/deep-thinking)
- [阿里云百炼 OpenAI-compatible Chat API](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions)
- [DeepSeek 首次 API 调用](https://api-docs.deepseek.com/)
- [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode)
- [DeepSeek Chat Completion 参数](https://api-docs.deepseek.com/api/create-chat-completion)

## 5. Fake Provider

`FakeScript` 是不可变步骤序列，每次构造模型都会获得独立游标。支持：

- `FakeTextStep`：固定文本、可选固定流式分片和非流式 Usage；
- `FakeToolCallStep`：固定工具名、结构化参数、调用 ID 和 Usage；
- `FakeFailureStep`：认证、权限、限流、超时、连接、上下文、上游和协议故障；
- 多步骤脚本：验证多轮工具调用和最终回答。

非流式 Usage 使用脚本中的明确数值；流式路径由 PydanticAI `FunctionModel` 对固定分片进行确定性 Usage 估算。Fake Provider 不伪装真实厂商，不允许回退到网络模型。

## 6. 错误与重试

Provider 响应会被转换为 AgentCell 的安全异常。异常只保留 Provider、模型名、状态码和通用说明，不保留响应体、请求头、API Key 或完整请求内容。普通协议拒绝会显示安全的 HTTP 状态码，便于区分请求 Schema 问题和认证、限流或上游错误。

| 场景 | 错误 | 默认可重试 |
| --- | --- | --- |
| 401 | `ProviderAuthenticationError` | 否 |
| 403 | `ProviderPermissionError` | 否 |
| 404 / 无效模型或端点 | `ProviderModelNotFoundError` | 否 |
| 上下文超限 | `ProviderContextLimitError` | 否 |
| 429 | `ProviderRateLimitError` | 是 |
| 连接失败、读取超时 | `ProviderConnectionError` / `ProviderTimeoutError` | 是 |
| 502、503、504 | 上游错误或超时 | 是 |
| 其他 5xx | `ProviderUpstreamError` | 否 |
| 无效参数或异常响应 | `ProviderProtocolError` | 否 |

`should_retry_provider_error` 只回答“该错误在剩余次数内是否允许重试”。真正的重试、退避、预算预留和 `model.failed` / `budget.updated` 事件必须由后续 RunService 统一编排，ProviderFactory 不会自行重试或静默切换模型。

## 7. HTTP 与密钥生命周期

- 调用方可注入共享 `httpx.AsyncClient`；注入的 Client 始终由调用方关闭；
- 未注入时，ProviderFactory 为模型创建具有独立超时和连接池的 Client，并在 `aclose()` 时关闭；
- `trust_env=False`，不会隐式继承系统代理；需要代理时只在 `http.proxy_env` 中声明环境变量名；
- 仅解析配置明确引用的环境变量，不读取或传递完整环境；
- 不跟随 HTTP 重定向，不记录 Authorization Header。

## 8. 测试与真实调用开关

默认契约测试将 PydanticAI 的 `ALLOW_MODEL_REQUESTS` 设为 `False`。真实测试只有同时存在明确开关和对应 API Key 时才运行：

```powershell
$env:AGENTCELL_RUN_LIVE_PROVIDER_TESTS="1"
$env:DASHSCOPE_API_KEY="..."
$env:DEEPSEEK_API_KEY="..."
uv run pytest tests/provider_contract/test_live_providers.py
```

该文件同时覆盖非流式纯文本、流式纯文本与一次真实 Function Calling。排查“文本可用但 Agent 工具请求被拒绝”时，应至少运行：

```powershell
uv run pytest tests/provider_contract/test_live_providers.py -k function_calling -vv
```

排查流式 SSE 或 Usage 映射时运行：

```powershell
uv run pytest tests/provider_contract/test_live_providers.py -k streaming -vv
```

仅配置 API Key 不会发起真实请求。CI 默认不得打开该开关。

## 9. 阶段 9.2.1 真实运行回归

真实运行已暴露两个必须由公共运行时修复、而不是厂商条件分支掩盖的问题：

- DeepSeek 可能把未解析 DSML 工具协议作为普通文本返回。若文本主体是 `<｜｜DSML｜｜tool_calls>`、`invoke`、未完成 Function Call 或继续调用不存在的 `artifact_list`，FinalOutputGuard 必须在 `run.completed` 前拒绝；运行时不得从该文本直接执行工具。先保证压缩摘要、真实工具目录和最终无工具重试一致；只有测试证明必须重新读取已引用大内容时，才增加受当前 Run/Conversation 引用范围约束的 `artifact.load`。一次预算内最终回答重试仍失败时返回 `invalid_final_output`。
- Qwen 可能为 `workspace.read` 自行估算 byte offset，并落在中文 UTF-8 continuation byte。读取器必须对齐合法边界并返回 requested/actual/next offset，不能把合法文件误报为 binary；真实非法字节仍拒绝。阶段 9.2.1 先完成兼容修复，只有实际出现文件版本竞争时再增加绑定哈希的 opaque Cursor。一次有界工具参数纠正后仍失败才终止 Run。

离线契约测试使用固定响应和 UTF-8 字节夹具覆盖上述路径；真实 Provider 冒烟测试只在显式开关下运行，并断言 Usage、事件、预算和终态均正确。公共 Kernel、Tool 和 FinalOutputGuard 只根据协议与数据形态判断，不得出现 DeepSeek/Qwen 名称分支。
