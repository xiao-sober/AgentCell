"""Explicitly gated smoke tests for paid Provider endpoints."""

from __future__ import annotations

import os
from collections.abc import AsyncIterable

import pytest
from pydantic_ai import Agent, AgentStreamEvent, RunContext, models

from agentcell.config import AgentCellSettings
from agentcell.providers import ProviderFactory

_LIVE_FLAG = "AGENTCELL_RUN_LIVE_PROVIDER_TESTS"


def _live_enabled() -> bool:
    return os.getenv(_LIVE_FLAG, "").casefold() in {"1", "true", "yes"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_ref", "key_env"),
    [
        ("qwen_plus", "DASHSCOPE_API_KEY"),
        ("deepseek_pro", "DEEPSEEK_API_KEY"),
    ],
)
async def test_live_provider_text_and_usage(model_ref: str, key_env: str) -> None:
    if not _live_enabled():
        pytest.skip(f"set {_LIVE_FLAG}=1 to allow paid Provider tests")
    if not os.getenv(key_env):
        pytest.skip(f"{key_env} is not configured")

    settings = AgentCellSettings.from_toml("agentcell.toml")
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = True
    try:
        async with ProviderFactory(settings.models) as factory:
            result = await Agent(await factory.build_model(model_ref)).run(
                "Reply with exactly: AgentCell provider contract OK"
            )
    finally:
        models.ALLOW_MODEL_REQUESTS = previous

    assert result.output
    assert result.usage.requests == 1
    assert result.usage.output_tokens > 0
    if model_ref == "deepseek_pro":
        cache_hits = result.usage.details.get("prompt_cache_hit_tokens")
        assert isinstance(cache_hits, int)
        assert result.usage.cache_read_tokens == cache_hits


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_ref", "key_env"),
    [
        ("qwen_plus", "DASHSCOPE_API_KEY"),
        ("deepseek_pro", "DEEPSEEK_API_KEY"),
    ],
)
async def test_live_provider_streaming_and_usage(model_ref: str, key_env: str) -> None:
    if not _live_enabled():
        pytest.skip(f"set {_LIVE_FLAG}=1 to allow paid Provider tests")
    if not os.getenv(key_env):
        pytest.skip(f"{key_env} is not configured")

    async def consume_stream(
        context: RunContext[object], events: AsyncIterable[AgentStreamEvent]
    ) -> None:
        del context
        async for _event in events:
            pass

    settings = AgentCellSettings.from_toml("agentcell.toml")
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = True
    try:
        async with ProviderFactory(settings.models) as factory:
            result = await Agent(await factory.build_model(model_ref)).run(
                "Reply with exactly: AgentCell streaming contract OK",
                event_stream_handler=consume_stream,
            )
    finally:
        models.ALLOW_MODEL_REQUESTS = previous

    assert result.output
    assert result.usage.requests == 1
    assert result.usage.output_tokens > 0
    if model_ref == "deepseek_pro":
        cache_hits = result.usage.details.get("prompt_cache_hit_tokens")
        assert isinstance(cache_hits, int)
        assert result.usage.cache_read_tokens == cache_hits


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_ref", "key_env"),
    [
        ("qwen_plus", "DASHSCOPE_API_KEY"),
        ("deepseek_pro", "DEEPSEEK_API_KEY"),
    ],
)
async def test_live_provider_function_calling(model_ref: str, key_env: str) -> None:
    if not _live_enabled():
        pytest.skip(f"set {_LIVE_FLAG}=1 to allow paid Provider tests")
    if not os.getenv(key_env):
        pytest.skip(f"{key_env} is not configured")

    settings = AgentCellSettings.from_toml("agentcell.toml")
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = True
    try:
        async with ProviderFactory(settings.models) as factory:
            agent = Agent(await factory.build_model(model_ref))

            def contract_value() -> str:
                """Return the exact value required by this provider contract test."""

                return "AGENTCELL_TOOL_OK"

            agent.tool_plain(contract_value)

            result = await agent.run(
                "Call contract_value exactly once, then reply with only its returned value."
            )
    finally:
        models.ALLOW_MODEL_REQUESTS = previous

    assert result.output.strip() == "AGENTCELL_TOOL_OK"
    assert result.usage.tool_calls == 1
