"""Offline contract checks for real adapter parameter mapping and client ownership."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable
from typing import cast

import httpx
import pytest
from pydantic_ai import Agent, AgentStreamEvent, RunContext, models
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from agentcell.config import AgentCellSettings
from agentcell.errors import ProviderConfigurationError
from agentcell.providers import EnvironmentSecretResolver, ProviderFactory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_ref", "expected_system", "expected_extra_body"),
    [
        (
            "qwen_plus",
            "alibaba",
            {"enable_thinking": True, "thinking_budget": 12_000},
        ),
        (
            "deepseek_pro",
            "deepseek",
            {"thinking": {"type": "enabled"}},
        ),
    ],
)
async def test_real_adapters_build_without_network_access(
    model_ref: str,
    expected_system: str,
    expected_extra_body: object,
) -> None:
    settings = AgentCellSettings.from_toml("agentcell.toml")
    secrets = EnvironmentSecretResolver(
        {
            "DASHSCOPE_API_KEY": "test-qwen-key",
            "DEEPSEEK_API_KEY": "test-deepseek-key",
        }
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    factory = ProviderFactory(settings.models, secret_resolver=secrets)

    model = await factory.build_model(model_ref, http_client=client)

    assert isinstance(model, OpenAIChatModel)
    assert model.system == expected_system
    assert model.settings is not None
    assert model.settings.get("extra_body") == expected_extra_body
    await factory.aclose()
    assert not client.is_closed
    await client.aclose()


@pytest.mark.asyncio
async def test_deepseek_reasoning_effort_is_mapped() -> None:
    settings = AgentCellSettings.from_toml("agentcell.toml")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    factory = ProviderFactory(
        settings.models,
        secret_resolver=EnvironmentSecretResolver({"DEEPSEEK_API_KEY": "test-key"}),
    )

    model = await factory.build_model("deepseek_pro", http_client=client)

    assert model.settings is not None
    model_settings = cast(OpenAIChatModelSettings, model.settings)
    assert model_settings.get("openai_reasoning_effort") == "high"
    await factory.aclose()
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_deepseek_cache_usage_is_mapped_for_both_response_modes(
    streamed: bool,
) -> None:
    usage: dict[str, int] = {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
        "prompt_cache_hit_tokens": 7,
        "prompt_cache_miss_tokens": 3,
    }

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            chunks: tuple[dict[str, object], ...] = (
                {
                    "id": "response-stream",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "deepseek-v4-pro",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                },
                {
                    "id": "response-stream",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "deepseek-v4-pro",
                    "choices": [],
                    "usage": usage,
                },
            )
            content = "".join(
                f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n" for chunk in chunks
            )
            return httpx.Response(
                200,
                content=f"{content}data: [DONE]\n\n".encode(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "id": "response-non-stream",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            },
        )

    settings = AgentCellSettings.from_toml("agentcell.toml")
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    factory = ProviderFactory(
        settings.models,
        secret_resolver=EnvironmentSecretResolver({"DEEPSEEK_API_KEY": "test-key"}),
    )
    agent = Agent(await factory.build_model("deepseek_pro", http_client=client))

    async def consume_stream(
        context: RunContext[object], events: AsyncIterable[AgentStreamEvent]
    ) -> None:
        del context
        async for _event in events:
            pass

    with models.override_allow_model_requests(True):
        if streamed:
            result = await agent.run("Reply with ok.", event_stream_handler=consume_stream)
        else:
            result = await agent.run("Reply with ok.")

    assert result.output == "ok"
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 2
    assert result.usage.cache_read_tokens == 7
    assert result.usage.cache_write_tokens == 0
    assert result.usage.details == {
        "prompt_cache_hit_tokens": 7,
        "prompt_cache_miss_tokens": 3,
    }

    await factory.aclose()
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("model_ref", ["qwen_plus", "deepseek_pro"])
async def test_tool_request_uses_provider_compatible_fields(model_ref: str) -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            message: dict[str, object] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "double", "arguments": '{"value":2}'},
                    }
                ],
            }
            if model_ref == "deepseek_pro":
                message["reasoning_content"] = "I should use the tool."
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": "4"}
            if model_ref == "deepseek_pro":
                message["reasoning_content"] = "The tool returned the answer."
            finish_reason = "stop"
        return httpx.Response(
            200,
            json={
                "id": f"response-{len(requests)}",
                "object": "chat.completion",
                "created": 1,
                "model": "contract-model",
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    settings = AgentCellSettings.from_toml("agentcell.toml")
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    factory = ProviderFactory(
        settings.models,
        secret_resolver=EnvironmentSecretResolver(
            {"DASHSCOPE_API_KEY": "test-qwen-key", "DEEPSEEK_API_KEY": "test-deepseek-key"}
        ),
    )
    agent = Agent(await factory.build_model(model_ref, http_client=client))

    def double(value: int) -> int:
        return value * 2

    agent.tool_plain(double)

    with models.override_allow_model_requests(True):
        result = await agent.run("Use the tool to double 2.")

    assert result.output == "4"
    assert len(requests) == 2
    first = requests[0]
    assert first["max_tokens"] == 16_000
    assert "max_completion_tokens" not in first
    if model_ref == "qwen_plus":
        assert first["parallel_tool_calls"] is False
    else:
        assert "parallel_tool_calls" not in first
    if model_ref == "deepseek_pro":
        assert "tool_choice" not in first
        second_messages = cast(list[dict[str, object]], requests[1]["messages"])
        assistant_message = next(item for item in second_messages if item["role"] == "assistant")
        assert assistant_message["reasoning_content"] == "I should use the tool."

    await factory.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_missing_api_key_fails_before_client_or_model_request() -> None:
    settings = AgentCellSettings.from_toml("agentcell.toml")
    factory = ProviderFactory(
        settings.models,
        secret_resolver=EnvironmentSecretResolver({}),
    )

    with pytest.raises(ProviderConfigurationError, match="DASHSCOPE_API_KEY"):
        await factory.build_model("qwen_plus")

    await factory.aclose()
