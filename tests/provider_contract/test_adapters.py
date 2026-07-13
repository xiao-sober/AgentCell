"""Offline contract checks for real adapter parameter mapping and client ownership."""

from __future__ import annotations

from typing import cast

import httpx
import pytest
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
async def test_missing_api_key_fails_before_client_or_model_request() -> None:
    settings = AgentCellSettings.from_toml("agentcell.toml")
    factory = ProviderFactory(
        settings.models,
        secret_resolver=EnvironmentSecretResolver({}),
    )

    with pytest.raises(ProviderConfigurationError, match="DASHSCOPE_API_KEY"):
        await factory.build_model("qwen_plus")

    await factory.aclose()
