"""Validated Provider configuration, usage, and normalized model output events."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, UrlConstraints, model_validator
from pydantic_ai.usage import RequestUsage, RunUsage

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type HttpsUrl = Annotated[AnyUrl, UrlConstraints(allowed_schemes=["https"])]


class ProviderName(StrEnum):
    """Provider identifiers allowed in model configuration."""

    BAILIAN = "bailian"
    DEEPSEEK = "deepseek"
    FAKE = "fake"


# Pydantic fields are statically mutable, while these models are frozen at runtime. Explicit
# covariance lets callers consume specialized Provider specs through the read-only base boundary.
ProviderT = TypeVar("ProviderT", bound=ProviderName, covariant=True)


class HttpClientSpec(BaseModel):
    """Per-model HTTP transport limits without embedding proxy credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proxy_env: str | None = Field(default=None, pattern=r"^[A-Z_][A-Z0-9_]*$")
    max_connections: int = Field(default=20, ge=1, le=1000, strict=True)
    max_keepalive_connections: int = Field(default=10, ge=0, le=1000, strict=True)
    keepalive_expiry_seconds: float = Field(default=5.0, gt=0, le=300)

    @model_validator(mode="after")
    def validate_pool_size(self) -> HttpClientSpec:
        if self.max_keepalive_connections > self.max_connections:
            raise ValueError("max_keepalive_connections cannot exceed max_connections")
        return self


class ModelSpec(BaseModel, Generic[ProviderT]):  # noqa: UP046
    """Provider-neutral fields shared by every configured model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderT
    model: str = Field(min_length=1)
    max_output_tokens: int = Field(default=4096, ge=1, strict=True)
    temperature: float | None = Field(default=None, ge=0, le=2)
    timeout_seconds: float = Field(default=120.0, gt=0, le=1800)
    max_retries: int = Field(default=3, ge=0, le=10, strict=True)
    http: HttpClientSpec = Field(default_factory=HttpClientSpec)


class NetworkModelSpec(ModelSpec[ProviderT], Generic[ProviderT]):  # noqa: UP046
    """Common configuration for Providers that require an environment credential."""

    api_key_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$")


class BailianModelSpec(NetworkModelSpec[Literal[ProviderName.BAILIAN]]):
    """Alibaba Cloud Model Studio settings interpreted only by its adapter."""

    provider: Literal[ProviderName.BAILIAN] = ProviderName.BAILIAN
    thinking: bool = True
    thinking_budget: int | None = Field(default=None, ge=1, strict=True)
    base_url: HttpsUrl | None = None

    @model_validator(mode="after")
    def validate_thinking_budget(self) -> BailianModelSpec:
        if not self.thinking and self.thinking_budget is not None:
            raise ValueError("thinking_budget requires thinking=true")
        return self


class DeepSeekModelSpec(NetworkModelSpec[Literal[ProviderName.DEEPSEEK]]):
    """DeepSeek official API settings interpreted only by its adapter."""

    provider: Literal[ProviderName.DEEPSEEK] = ProviderName.DEEPSEEK
    thinking: bool = True
    reasoning_effort: Literal["high", "max"] | None = "high"

    @model_validator(mode="after")
    def validate_thinking_settings(self) -> DeepSeekModelSpec:
        if self.thinking and self.temperature is not None:
            raise ValueError("DeepSeek thinking mode does not support temperature")
        if not self.thinking and self.reasoning_effort is not None:
            raise ValueError("reasoning_effort requires thinking=true")
        return self


class FakeModelSpec(ModelSpec[Literal[ProviderName.FAKE]]):
    """Offline model reference resolved through a registered deterministic script."""

    provider: Literal[ProviderName.FAKE] = ProviderName.FAKE


type ModelSpecDefinition = Annotated[
    BailianModelSpec | DeepSeekModelSpec | FakeModelSpec,
    Field(discriminator="provider"),
]


type NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]


class ModelUsage(BaseModel):
    """Provider-independent token and request counters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requests: int = Field(default=0, ge=0, strict=True)
    tool_calls: int = Field(default=0, ge=0, strict=True)
    input_tokens: int = Field(default=0, ge=0, strict=True)
    cache_write_tokens: int = Field(default=0, ge=0, strict=True)
    cache_read_tokens: int = Field(default=0, ge=0, strict=True)
    output_tokens: int = Field(default=0, ge=0, strict=True)
    details: dict[str, NonNegativeInt] = Field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        """Return PydanticAI-compatible input plus output token usage."""

        return self.input_tokens + self.output_tokens

    @classmethod
    def from_pydantic_ai(cls, usage: RequestUsage | RunUsage) -> ModelUsage:
        """Normalize PydanticAI request or full-run usage without Provider branches."""

        requests = usage.requests if isinstance(usage, RunUsage) else 0
        tool_calls = usage.tool_calls if isinstance(usage, RunUsage) else 0
        return cls(
            requests=requests,
            tool_calls=tool_calls,
            input_tokens=usage.input_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            output_tokens=usage.output_tokens,
            details=dict(usage.details),
        )

    def to_request_usage(self) -> RequestUsage:
        """Convert scripted Fake usage to the PydanticAI model boundary."""

        return RequestUsage(
            input_tokens=self.input_tokens,
            cache_write_tokens=self.cache_write_tokens,
            cache_read_tokens=self.cache_read_tokens,
            output_tokens=self.output_tokens,
            details=dict(self.details),
        )


class ModelTextDelta(BaseModel):
    """Normalized streamed model text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["text_delta"] = "text_delta"
    delta: str = Field(min_length=1)


class ModelToolCall(BaseModel):
    """Normalized complete function call proposed by a model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["tool_call"] = "tool_call"
    tool_name: str = Field(min_length=1)
    arguments: str | dict[str, JsonValue]
    tool_call_id: str = Field(min_length=1)


class ModelCompleted(BaseModel):
    """Normalized terminal output carrying usage and finish information."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["completed"] = "completed"
    usage: ModelUsage
    finish_reason: str | None = None


type ModelOutputEvent = Annotated[
    ModelTextDelta | ModelToolCall | ModelCompleted,
    Field(discriminator="kind"),
]


class FakeFailureKind(StrEnum):
    """Classified failures a Fake script can inject without network access."""

    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    CONTEXT_LIMIT = "context_limit"
    UPSTREAM = "upstream"
    PROTOCOL = "protocol"
