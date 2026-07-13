"""DeepSeek official API adapter using PydanticAI's dedicated Provider."""

from __future__ import annotations

import httpx
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.deepseek import DeepSeekProvider

from agentcell.errors import ProviderConfigurationError
from agentcell.providers.models import DeepSeekModelSpec, ModelSpec, ProviderName


class DeepSeekProviderAdapter:
    """Map DeepSeek V4 thinking controls while keeping the official endpoint fixed."""

    provider = ProviderName.DEEPSEEK
    requires_api_key = True
    uses_http_client = True
    cacheable = True

    def build_model(
        self,
        spec: ModelSpec[ProviderName],
        *,
        api_key: str | None,
        http_client: httpx.AsyncClient | None,
    ) -> Model:
        if not isinstance(spec, DeepSeekModelSpec):
            raise ProviderConfigurationError(
                "DeepSeek adapter requires DeepSeekModelSpec",
                provider=self.provider,
                model=spec.model,
            )
        if not api_key or http_client is None:
            raise ProviderConfigurationError(
                "DeepSeek adapter requires an API key and HTTP client",
                provider=self.provider,
                model=spec.model,
            )

        thinking_type = "enabled" if spec.thinking else "disabled"
        settings = OpenAIChatModelSettings(
            max_tokens=spec.max_output_tokens,
            timeout=spec.timeout_seconds,
            extra_body={"thinking": {"type": thinking_type}},
        )
        if spec.reasoning_effort is not None:
            settings["openai_reasoning_effort"] = spec.reasoning_effort
        if spec.temperature is not None:
            settings["temperature"] = spec.temperature

        provider = DeepSeekProvider(api_key=api_key, http_client=http_client)
        return OpenAIChatModel(spec.model, provider=provider, settings=settings)
