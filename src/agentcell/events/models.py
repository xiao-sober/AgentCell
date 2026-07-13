"""Versioned, immutable domain event models and payload redaction helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

REDACTED_VALUE = "[REDACTED]"

_SENSITIVE_KEYS = frozenset(
    {
        "apikey",
        "authorization",
        "proxyauthorization",
        "password",
        "secret",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "cookie",
        "setcookie",
        "privatekey",
        "credentials",
        "xapikey",
    }
)


class EventType(StrEnum):
    """Stable names for the core AgentCell domain event stream."""

    RUN_STARTED = "run.started"
    RUN_STATUS_CHANGED = "run.status_changed"
    MODEL_REQUESTED = "model.requested"
    MODEL_TEXT_DELTA = "model.text_delta"
    MODEL_COMPLETED = "model.completed"
    MODEL_FAILED = "model.failed"
    TOOL_PROPOSED = "tool.proposed"
    TOOL_APPROVAL_REQUIRED = "tool.approval_required"
    TOOL_APPROVED = "tool.approved"
    TOOL_REJECTED = "tool.rejected"
    TOOL_STARTED = "tool.started"
    TOOL_OUTPUT_DELTA = "tool.output_delta"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    AGENT_CHILD_STARTED = "agent.child_started"
    AGENT_CHILD_COMPLETED = "agent.child_completed"
    MEMORY_RECALLED = "memory.recalled"
    MEMORY_WRITTEN = "memory.written"
    CONTEXT_COMPACTED = "context.compacted"
    BUDGET_UPDATED = "budget.updated"
    CHECKPOINT_CREATED = "checkpoint.created"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"


def _normalize_key(key: str) -> str:
    return "".join(character for character in key.casefold() if character.isalnum())


def redact_sensitive_data(value: JsonValue) -> JsonValue:
    """Return a recursive copy with known credential fields replaced."""

    if isinstance(value, dict):
        redacted: dict[str, JsonValue] = {}
        for key, item in value.items():
            redacted[key] = (
                REDACTED_VALUE
                if _normalize_key(key) in _SENSITIVE_KEYS
                else redact_sensitive_data(item)
            )
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    return value


class EventPayload(BaseModel):
    """Base for versioned event payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1, strict=True)

    def safe_dump(self) -> dict[str, JsonValue]:
        """Serialize this payload with recursive credential redaction."""

        raw = cast(dict[str, JsonValue], self.model_dump(mode="json"))
        return cast(dict[str, JsonValue], redact_sensitive_data(raw))


class GenericEventPayload(EventPayload):
    """Versioned payload for event types whose dedicated schema is not defined yet."""

    data: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("data")
    @classmethod
    def redact_data(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], redact_sensitive_data(value))


class TextDeltaPayload(EventPayload):
    """A non-empty model or tool output delta."""

    delta: str = Field(min_length=1)


class ErrorPayload(EventPayload):
    """Sanitized failure information safe for events and product surfaces."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def redact_details(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], redact_sensitive_data(value))


class RunStartedPayload(EventPayload):
    """Initial identity recorded when a Run is created."""

    conversation_id: UUID
    agent_id: str = Field(min_length=1)


class RunStatusChangedPayload(EventPayload):
    """One validated lifecycle transition without depending on kernel enums."""

    previous_status: str = Field(min_length=1)
    status: str = Field(min_length=1)


class ModelRequestedPayload(EventPayload):
    """Sanitized identity for one budget-reserved model request."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    request_index: int = Field(ge=1, strict=True)


class ModelCompletedPayload(EventPayload):
    """Normalized usage for one completed model request."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    request_index: int = Field(ge=1, strict=True)
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    cache_write_tokens: int = Field(default=0, ge=0, strict=True)
    cache_read_tokens: int = Field(default=0, ge=0, strict=True)


class RunCompletedPayload(EventPayload):
    """Terminal output metadata kept small enough for the event stream."""

    output_characters: int = Field(ge=0, strict=True)
    requests: int = Field(ge=0, strict=True)
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    tool_calls: int = Field(ge=0, strict=True)


class ArtifactReference(BaseModel):
    """Stable reference embedded in events when content is stored outside the event table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: UUID
    media_type: str = Field(min_length=1)
    size_bytes: int = Field(ge=0, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DomainEvent[PayloadT: EventPayload](BaseModel):
    """Append-only event envelope with a Run-local monotonic sequence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    sequence: int = Field(ge=1, strict=True)
    event_type: EventType
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: PayloadT

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)

    def safe_payload(self) -> dict[str, JsonValue]:
        """Return the event payload in its persistence-safe redacted form."""

        return self.payload.safe_dump()


_PAYLOAD_MODELS: dict[EventType, type[EventPayload]] = {
    EventType.RUN_STARTED: RunStartedPayload,
    EventType.RUN_STATUS_CHANGED: RunStatusChangedPayload,
    EventType.MODEL_REQUESTED: ModelRequestedPayload,
    EventType.MODEL_TEXT_DELTA: TextDeltaPayload,
    EventType.MODEL_COMPLETED: ModelCompletedPayload,
    EventType.TOOL_OUTPUT_DELTA: TextDeltaPayload,
    EventType.MODEL_FAILED: ErrorPayload,
    EventType.TOOL_FAILED: ErrorPayload,
    EventType.RUN_FAILED: ErrorPayload,
    EventType.RUN_COMPLETED: RunCompletedPayload,
}


def payload_model_for(event_type: EventType) -> type[EventPayload]:
    """Return the registered payload model for an event type and current version."""

    return _PAYLOAD_MODELS.get(event_type, GenericEventPayload)


def parse_event_payload(
    event_type: EventType,
    data: dict[str, JsonValue],
) -> EventPayload:
    """Restore persisted payload data using the event type's current schema."""

    return payload_model_for(event_type).model_validate(data)
