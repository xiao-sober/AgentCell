"""Durable file-change domain models independent of Git and transports."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentcell.events import ArtifactReference


class ChangeSetStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CONFLICT = "conflict"
    REVERTED = "reverted"


class FileChangeStatus(StrEnum):
    PREPARED = "prepared"
    APPLIED = "applied"
    COMPLETED = "completed"
    CONFLICT = "conflict"
    FAILED = "failed"
    REVERTED = "reverted"


class FileOperation(StrEnum):
    CREATED = "created"
    REPLACED = "replaced"
    PATCHED = "patched"
    DELETED = "deleted"
    REVERTED = "reverted"


class GitBaseline(BaseModel):
    """Optional, bounded Git metadata; never required for correctness."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_root: str | None = None
    head: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    branch: str | None = Field(default=None, max_length=255)
    dirty: bool = False
    path_status: str | None = Field(default=None, max_length=1_000)


class ChangeSet(BaseModel):
    """One Run-scoped ordered group of AgentCell-owned file changes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    conversation_id: UUID
    agent_id: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    git: GitBaseline | None = None
    status: ChangeSetStatus = ChangeSetStatus.ACTIVE
    source_change_set_id: UUID | None = None
    storage_bytes: int = Field(default=0, ge=0, strict=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    @field_validator("created_at", "completed_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("change-set timestamps must be timezone-aware")
        return value.astimezone(UTC)


class FileChange(BaseModel):
    """One immutable-intent file transition with mutable recovery projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    change_set_id: UUID
    run_id: UUID
    conversation_id: UUID
    agent_id: str = Field(min_length=1)
    provider_call_id: str | None = Field(default=None, min_length=1)
    approval_id: UUID | None = None
    path: str = Field(min_length=1, max_length=2_048)
    operation: FileOperation
    before_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    after_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    before_artifact: ArtifactReference | None = None
    after_artifact: ArtifactReference | None = None
    diff_artifact: ArtifactReference
    git_diff_artifact: ArtifactReference | None = None
    git_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    git_dirty_before: bool = False
    status: FileChangeStatus = FileChangeStatus.PREPARED
    reverts_change_id: UUID | None = None
    reverted_by_change_id: UUID | None = None
    storage_bytes: int = Field(default=0, ge=0, strict=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    applied_at: datetime | None = None
    completed_at: datetime | None = None

    @field_validator("created_at", "applied_at", "completed_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("file-change timestamps must be timezone-aware")
        return value.astimezone(UTC)


class ChangeDetails(BaseModel):
    """Stable query projection containing one change and its full Diff."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change_set: ChangeSet
    change: FileChange
    diff: str
