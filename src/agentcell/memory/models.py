"""Four-layer memory domain models and scoped retrieval results."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MemoryKind(StrEnum):
    WORKING = "working"
    CONVERSATION = "conversation"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


class MemoryScope(BaseModel):
    """Explicit user/project/Agent boundary used for reads and writes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: UUID
    project_id: str = Field(min_length=1, max_length=512)
    agent_id: str | None = Field(default=None, min_length=1, max_length=255)


class MemoryItem(BaseModel):
    """One durable, editable memory fact or episode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    kind: MemoryKind
    scope: MemoryScope
    content: str = Field(min_length=1, max_length=64_000)
    tags: frozenset[str] = frozenset()
    importance: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    sensitive: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> frozenset[str]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("tags must be a collection")
        items = cast(Iterable[object], value)
        normalized = {str(item).strip().casefold() for item in items if str(item).strip()}
        if len(normalized) > 32 or any(len(item) > 64 for item in normalized):
            raise ValueError("tags exceed count or length limit")
        return frozenset(normalized)

    @field_validator("created_at", "updated_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("memory timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_times(self) -> MemoryItem:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self

    def is_expired(self, *, at: datetime | None = None) -> bool:
        now = at or datetime.now(UTC)
        return self.expires_at is not None and self.expires_at <= now


class MemoryCandidate(BaseModel):
    """Proposed memory write evaluated by MemoryPolicy before persistence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: MemoryKind
    scope: MemoryScope
    content: str = Field(min_length=1, max_length=64_000)
    tags: frozenset[str] = frozenset()
    importance: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    expires_at: datetime | None = None


class MemorySearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item: MemoryItem
    score: float = Field(ge=0, allow_inf_nan=False)
    bm25_relevance: float = Field(ge=0, allow_inf_nan=False)
    time_decay: float = Field(ge=0, le=1, allow_inf_nan=False)
    tag_overlap: float = Field(ge=0, le=1, allow_inf_nan=False)
