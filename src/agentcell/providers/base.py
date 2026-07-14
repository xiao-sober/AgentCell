"""Small Provider adapter boundary, HTTP client factory, and safe error classification."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol, cast

import httpx
import openai
from pydantic_ai import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model

from agentcell.errors import (
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderContextLimitError,
    ProviderError,
    ProviderModelNotFoundError,
    ProviderPermissionError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUpstreamError,
)
from agentcell.providers.models import (
    JsonValue,
    ModelCompleted,
    ModelOutputEvent,
    ModelSpec,
    ModelTextDelta,
    ModelToolCall,
    ModelUsage,
    NetworkModelSpec,
    ProviderName,
)


class SecretResolver(Protocol):
    """Resolve a named secret without exposing the surrounding environment."""

    def resolve(self, name: str) -> str:
        """Return one non-empty secret or raise a sanitized configuration error."""

        ...


class EnvironmentSecretResolver:
    """Resolve only explicitly requested environment variable names."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def resolve(self, name: str) -> str:
        value = self._environ.get(name, "").strip()
        if not value:
            raise ProviderConfigurationError(f"Required environment variable {name!r} is not set")
        return value


class ProviderAdapter(Protocol):
    """Build a PydanticAI model while keeping vendor mapping in one module."""

    provider: ProviderName
    requires_api_key: bool
    uses_http_client: bool
    cacheable: bool

    def build_model(
        self,
        spec: ModelSpec[ProviderName],
        *,
        api_key: str | None,
        http_client: httpx.AsyncClient | None,
    ) -> Model:
        """Build a model for one already validated specification."""

        ...


def create_provider_http_client(
    spec: NetworkModelSpec[ProviderName],
    *,
    proxy: str | None = None,
) -> httpx.AsyncClient:
    """Create an owned, per-model client with explicit timeout and pool limits."""

    limits = httpx.Limits(
        max_connections=spec.http.max_connections,
        max_keepalive_connections=spec.http.max_keepalive_connections,
        keepalive_expiry=spec.http.keepalive_expiry_seconds,
    )
    return httpx.AsyncClient(
        timeout=httpx.Timeout(spec.timeout_seconds),
        limits=limits,
        proxy=proxy,
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "AgentCell/0.1"},
    )


def model_response_events(response: ModelResponse) -> tuple[ModelOutputEvent, ...]:
    """Normalize a complete PydanticAI response for RunService event mapping."""

    events: list[ModelOutputEvent] = []
    for part in response.parts:
        if isinstance(part, TextPart) and part.content:
            events.append(ModelTextDelta(delta=part.content))
        elif isinstance(part, ToolCallPart):
            arguments = cast(str | dict[str, JsonValue], part.args or {})
            events.append(
                ModelToolCall(
                    tool_name=part.tool_name,
                    arguments=arguments,
                    tool_call_id=part.tool_call_id,
                )
            )
    events.append(
        ModelCompleted(
            usage=ModelUsage.from_pydantic_ai(response.usage),
            finish_reason=response.finish_reason,
        )
    )
    return tuple(events)


def should_retry_provider_error(error: ProviderError, *, attempt: int, max_retries: int) -> bool:
    """Apply the shared retry ceiling only to errors classified as retryable."""

    if attempt < 0 or max_retries < 0:
        raise ValueError("attempt and max_retries must be non-negative")
    return error.retryable and attempt < max_retries


def classify_provider_error(provider: str, model: str, error: BaseException) -> ProviderError:
    """Map Provider and transport exceptions without retaining bodies, headers, or keys."""

    for current in _exception_chain(error):
        if isinstance(current, ProviderError):
            return current
        if isinstance(current, ModelHTTPError):
            return _classify_http_status(
                provider,
                model,
                status_code=current.status_code,
                body=current.body,
            )
        if isinstance(current, openai.APIStatusError):
            return _classify_http_status(
                provider,
                model,
                status_code=current.status_code,
                body=current.body,
            )
        if isinstance(
            current,
            (httpx.TimeoutException, openai.APITimeoutError, TimeoutError),
        ):
            return ProviderTimeoutError(provider, model, "request timed out")
        if isinstance(
            current,
            (httpx.NetworkError, openai.APIConnectionError),
        ):
            return ProviderConnectionError(provider, model, "connection failed")
        if isinstance(current, UnexpectedModelBehavior):
            return ProviderProtocolError(provider, model, "unexpected model response")
    return ProviderProtocolError(provider, model, "unclassified Provider failure")


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _classify_http_status(
    provider: str,
    model: str,
    *,
    status_code: int,
    body: object | None,
) -> ProviderError:
    if status_code == 401:
        return ProviderAuthenticationError(
            provider, model, "authentication was rejected", status_code=status_code
        )
    if status_code == 403:
        return ProviderPermissionError(
            provider, model, "access was forbidden", status_code=status_code
        )
    if status_code == 404:
        return ProviderModelNotFoundError(
            provider, model, "model or endpoint was not found", status_code=status_code
        )
    if status_code in {408, 504}:
        return ProviderTimeoutError(
            provider, model, "upstream request timed out", status_code=status_code
        )
    if status_code == 429:
        return ProviderRateLimitError(
            provider, model, "rate limit exceeded", status_code=status_code
        )
    if status_code in {400, 413, 422} and _looks_like_context_limit(body):
        return ProviderContextLimitError(
            provider, model, "model context limit exceeded", status_code=status_code
        )
    if status_code >= 500:
        return ProviderUpstreamError(
            provider,
            model,
            "upstream service failed",
            status_code=status_code,
            retryable=status_code in {502, 503},
        )
    return ProviderProtocolError(
        provider,
        model,
        f"request was rejected (HTTP {status_code})",
        status_code=status_code,
    )


def _looks_like_context_limit(body: object | None) -> bool:
    normalized = str(body).casefold().replace("-", "_")
    markers = (
        "context_length_exceeded",
        "context limit",
        "context window",
        "maximum context length",
        "too many tokens",
    )
    return any(marker in normalized for marker in markers)
