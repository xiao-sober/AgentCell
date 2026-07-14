"""DeepSeek official API adapter using PydanticAI's dedicated Provider."""

from __future__ import annotations

from dataclasses import replace

import httpx
from openai.types import chat
from openai.types.chat import ChatCompletionToolChoiceOptionParam
from openai.types.completion_usage import CompletionUsage
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.openai import (
    OpenAIChatModel,
    OpenAIChatModelSettings,
    OpenAIStreamedResponse,
)
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.usage import RequestUsage

from agentcell.errors import ProviderConfigurationError
from agentcell.providers.models import DeepSeekModelSpec, ModelSpec, ProviderName


def _map_deepseek_cache_usage(
    mapped: RequestUsage,
    response_usage: CompletionUsage | None,
) -> RequestUsage:
    """Map DeepSeek's top-level context-cache fields onto PydanticAI usage."""

    if response_usage is None:
        return mapped
    value = response_usage.model_dump(exclude_none=True).get("prompt_cache_hit_tokens")
    if not isinstance(value, int) or isinstance(value, bool):
        return mapped
    cache_read_tokens = max(0, value)
    if mapped.input_tokens > 0:
        cache_read_tokens = min(cache_read_tokens, mapped.input_tokens)
    return replace(mapped, cache_read_tokens=cache_read_tokens)


class DeepSeekStreamedResponse(OpenAIStreamedResponse):
    """Map DeepSeek usage from the final usage-only SSE chunk."""

    def _map_usage(self, response: chat.ChatCompletionChunk) -> RequestUsage:
        mapped = super()._map_usage(response)
        return _map_deepseek_cache_usage(mapped, response.usage)


class DeepSeekChatModel(OpenAIChatModel):
    """Apply DeepSeek V4's tool and usage contracts."""

    def _get_tool_choice(
        self,
        model_settings: OpenAIChatModelSettings,
        model_request_parameters: ModelRequestParameters,
    ) -> tuple[list[chat.ChatCompletionToolParam], ChatCompletionToolChoiceOptionParam | None]:
        tools, _tool_choice = super()._get_tool_choice(
            model_settings,
            model_request_parameters,
        )
        # DeepSeek V4 accepts tool definitions in thinking mode but rejects the
        # OpenAI-compatible tool_choice field, including the default "auto" value.
        return tools, None

    @property
    def _streamed_response_cls(self) -> type[OpenAIStreamedResponse]:
        return DeepSeekStreamedResponse

    def _map_usage(self, response: chat.ChatCompletion) -> RequestUsage:
        mapped = super()._map_usage(response)
        return _map_deepseek_cache_usage(mapped, response.usage)


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
        profile = OpenAIModelProfile(
            **(provider.model_profile(spec.model) or {}),
            openai_chat_supports_max_completion_tokens=False,
        )
        return DeepSeekChatModel(
            spec.model,
            provider=provider,
            profile=profile,
            settings=settings,
        )
