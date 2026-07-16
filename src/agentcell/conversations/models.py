"""Conversation domain models and sanitized ordered message projections."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentcell.events import JsonValue


class ConversationMessageKind(StrEnum):
    REQUEST = "request"
    RESPONSE = "response"


class ConversationRoutingMode(StrEnum):
    FIXED = "fixed"
    AUTO = "auto"


class Conversation(BaseModel):
    """Stable scope shared by an ordered sequence of otherwise independent Runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    project_id: str = Field(min_length=1, max_length=512)
    workspace: str = Field(min_length=1, max_length=2048)
    agent_id: str = Field(min_length=1, max_length=255)
    routing_mode: ConversationRoutingMode = ConversationRoutingMode.FIXED
    team_id: str | None = Field(default=None, min_length=1, max_length=255)
    routing_policy_version: str | None = Field(default=None, min_length=1, max_length=64)
    model_ref: str | None = Field(default=None, min_length=1, max_length=255)
    title: str | None = Field(default=None, max_length=255)
    active_run_id: UUID | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Conversation timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_routing_binding(self) -> Conversation:
        if self.routing_mode is ConversationRoutingMode.AUTO:
            if self.agent_id != "task-router" or self.team_id is not None:
                raise ValueError("auto Conversation must bind to task-router without a fixed Team")
            if self.routing_policy_version is None:
                raise ValueError("auto Conversation requires routing_policy_version")
        elif self.routing_policy_version is not None:
            raise ValueError("fixed Conversation cannot bind a RoutingPolicy version")
        return self


class ConversationMessage(BaseModel):
    """One append-only, sanitized PydanticAI message in a Conversation thread."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    run_id: UUID
    sequence: int = Field(ge=1, strict=True)
    kind: ConversationMessageKind
    payload_version: int = Field(default=1, ge=1, strict=True)
    payload: dict[str, JsonValue]
    artifact_ids: tuple[UUID, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)
