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
