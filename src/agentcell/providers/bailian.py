"""Alibaba Cloud Model Studio adapter using PydanticAI's OpenAI-compatible model."""

from __future__ import annotations

import httpx
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.alibaba import AlibabaProvider

from agentcell.errors import ProviderConfigurationError
from agentcell.providers.models import BailianModelSpec, ModelSpec, ProviderName


class BailianProviderAdapter:
    """Map typed Bailian-only parameters to the DashScope-compatible request."""

    provider = ProviderName.BAILIAN
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
        if not isinstance(spec, BailianModelSpec):
            raise ProviderConfigurationError(
                "Bailian adapter requires BailianModelSpec",
                provider=self.provider,
                model=spec.model,
            )
        if not api_key or http_client is None:
            raise ProviderConfigurationError(
                "Bailian adapter requires an API key and HTTP client",
                provider=self.provider,
                model=spec.model,
            )

        extra_body: dict[str, object] = {"enable_thinking": spec.thinking}
        if spec.thinking_budget is not None:
            extra_body["thinking_budget"] = spec.thinking_budget

        settings = OpenAIChatModelSettings(
            max_tokens=spec.max_output_tokens,
            timeout=spec.timeout_seconds,
            extra_body=extra_body,
        )
        if spec.temperature is not None:
            settings["temperature"] = spec.temperature

        provider = AlibabaProvider(
            api_key=api_key,
            base_url=str(spec.base_url) if spec.base_url is not None else None,
            http_client=http_client,
        )
        return OpenAIChatModel(spec.model, provider=provider, settings=settings)
