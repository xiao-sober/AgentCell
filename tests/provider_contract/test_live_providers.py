"""Explicitly gated smoke tests for paid Provider endpoints."""

from __future__ import annotations

import os

import pytest
from pydantic_ai import Agent, models

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
