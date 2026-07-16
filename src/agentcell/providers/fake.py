"""Deterministic offline Provider built on PydanticAI's FunctionModel test boundary."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from agentcell.errors import (
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderContextLimitError,
    ProviderError,
    ProviderPermissionError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUpstreamError,
)
from agentcell.providers.models import (
    FakeFailureKind,
    FakeModelSpec,
    JsonValue,
    ModelSpec,
    ModelUsage,
    ProviderName,
)
from agentcell.providers.tool_names import portable_tool_name


class FakeTextStep(BaseModel):
    """One deterministic text response and its non-streamed usage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["text"] = "text"
    text: str = Field(min_length=1)
    chunks: tuple[str, ...] | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)

    @model_validator(mode="after")
    def validate_chunks(self) -> FakeTextStep:
        if self.chunks is not None:
            if not self.chunks or any(not chunk for chunk in self.chunks):
                raise ValueError("chunks must contain non-empty strings")
            if "".join(self.chunks) != self.text:
                raise ValueError("chunks must concatenate to text")
        return self

    def stream_chunks(self) -> tuple[str, ...]:
        return self.chunks or (self.text,)


class FakeToolCallStep(BaseModel):
    """One deterministic function call response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["tool_call"] = "tool_call"
    tool_name: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    tool_call_id: str = Field(default="fake-tool-call-1", min_length=1)
    usage: ModelUsage = Field(default_factory=ModelUsage)


class FakeToolCallsStep(BaseModel):
    """Multiple deterministic function calls returned in one model response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["tool_calls"] = "tool_calls"
    calls: tuple[FakeToolCallStep, ...] = Field(min_length=2)
    usage: ModelUsage = Field(default_factory=ModelUsage)

    @model_validator(mode="after")
    def validate_call_ids(self) -> FakeToolCallsStep:
        call_ids = tuple(call.tool_call_id for call in self.calls)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("calls must use unique tool_call_id values")
        return self


class FakeFailureStep(BaseModel):
    """One deterministic, already classified Provider failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["failure"] = "failure"
    failure: FakeFailureKind


type FakeStep = Annotated[
    FakeTextStep | FakeToolCallStep | FakeToolCallsStep | FakeFailureStep,
    Field(discriminator="kind"),
]


class FakeScript(BaseModel):
    """Immutable response plan; every built model receives a fresh cursor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: tuple[FakeStep, ...] = Field(min_length=1)


@dataclass
class _ScriptCursor:
    script: FakeScript
    index: int = 0

    def next_step(self, *, model: str) -> FakeStep:
        try:
            step = self.script.steps[self.index]
        except IndexError as error:
            raise ProviderProtocolError(
                ProviderName.FAKE,
                model,
                "deterministic script was exhausted",
            ) from error
        self.index += 1
        return step


class FakeProviderAdapter:
    """Build isolated deterministic model sessions from named scripts."""

    provider = ProviderName.FAKE
    requires_api_key = False
    uses_http_client = False
    cacheable = False

    def __init__(self, scripts: Mapping[str, FakeScript] | None = None) -> None:
        self._scripts = dict(scripts or {})

    def build_model(
        self,
        spec: ModelSpec[ProviderName],
        *,
        api_key: str | None,
        http_client: httpx.AsyncClient | None,
    ) -> Model:
        del api_key, http_client
        if not isinstance(spec, FakeModelSpec):
            raise ProviderConfigurationError(
                "Fake adapter requires FakeModelSpec",
                provider=self.provider,
                model=spec.model,
            )
        try:
            script = self._scripts[spec.model]
        except KeyError as error:
            raise ProviderConfigurationError(
                f"No Fake script is registered for model {spec.model!r}",
                provider=self.provider,
                model=spec.model,
            ) from error

        cursor = _ScriptCursor(script)

        async def request(
            messages: list[ModelMessage],
            agent_info: AgentInfo,
        ) -> ModelResponse:
            del messages
            step = cursor.next_step(model=spec.model)
            if isinstance(step, FakeFailureStep):
                raise _fake_failure(step.failure, model=spec.model)
            if isinstance(step, FakeTextStep):
                return ModelResponse(
                    parts=[TextPart(step.text)],
                    usage=step.usage.to_request_usage(),
                    finish_reason="stop",
                )
            if isinstance(step, FakeToolCallsStep):
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            _resolve_script_tool_name(call.tool_name, agent_info),
                            call.arguments,
                            call.tool_call_id,
                        )
                        for call in step.calls
                    ],
                    usage=step.usage.to_request_usage(),
                    finish_reason="tool_call",
                )
            tool_name = _resolve_script_tool_name(step.tool_name, agent_info)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name,
                        step.arguments,
                        step.tool_call_id,
                    )
                ],
                usage=step.usage.to_request_usage(),
                finish_reason="tool_call",
            )

        async def request_stream(
            messages: list[ModelMessage],
            agent_info: AgentInfo,
        ) -> AsyncIterator[str | DeltaToolCalls]:
            del messages
            step = cursor.next_step(model=spec.model)
            if isinstance(step, FakeFailureStep):
                raise _fake_failure(step.failure, model=spec.model)
            if isinstance(step, FakeTextStep):
                for chunk in step.stream_chunks():
                    yield chunk
                return
            if isinstance(step, FakeToolCallsStep):
                yield {
                    index: DeltaToolCall(
                        name=_resolve_script_tool_name(call.tool_name, agent_info),
                        json_args=json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        tool_call_id=call.tool_call_id,
                    )
                    for index, call in enumerate(step.calls)
                }
                return
            tool_name = _resolve_script_tool_name(step.tool_name, agent_info)
            yield {
                0: DeltaToolCall(
                    name=tool_name,
                    json_args=json.dumps(step.arguments, ensure_ascii=False, separators=(",", ":")),
                    tool_call_id=step.tool_call_id,
                )
            }

        return FunctionModel(
            request,
            stream_function=request_stream,
            model_name=spec.model,
        )


def _resolve_script_tool_name(name: str, agent_info: AgentInfo) -> str:
    available = {tool.name for tool in agent_info.function_tools}
    if name in available:
        return name
    alias = portable_tool_name(name)
    return alias if alias in available else name


def _fake_failure(kind: FakeFailureKind, *, model: str) -> ProviderError:
    provider = ProviderName.FAKE
    if kind is FakeFailureKind.AUTHENTICATION:
        return ProviderAuthenticationError(provider, model, "authentication was rejected")
    if kind is FakeFailureKind.PERMISSION:
        return ProviderPermissionError(provider, model, "access was forbidden")
    if kind is FakeFailureKind.RATE_LIMIT:
        return ProviderRateLimitError(provider, model, "rate limit exceeded")
    if kind is FakeFailureKind.TIMEOUT:
        return ProviderTimeoutError(provider, model, "request timed out")
    if kind is FakeFailureKind.CONNECTION:
        return ProviderConnectionError(provider, model, "connection failed")
    if kind is FakeFailureKind.CONTEXT_LIMIT:
        return ProviderContextLimitError(provider, model, "model context limit exceeded")
    if kind is FakeFailureKind.UPSTREAM:
        return ProviderUpstreamError(
            provider,
            model,
            "upstream service failed",
            status_code=503,
            retryable=True,
        )
    return ProviderProtocolError(provider, model, "unexpected model response")
