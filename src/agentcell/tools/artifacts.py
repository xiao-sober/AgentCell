"""Artifact metadata independent of its persistence implementation."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ArtifactMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    storage_key: str = Field(min_length=1, max_length=512)
    suggested_name: str = Field(min_length=1, max_length=255)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)
