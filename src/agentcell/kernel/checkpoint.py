"""Checkpoint domain model containing only restart-safe serialized state."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentcell.budgets import BudgetSnapshot
from agentcell.events import JsonValue
from agentcell.kernel.lifecycle import RunStatus
from agentcell.policy import CapabilityLease, PermissionMode


class CheckpointKind(StrEnum):
    APPROVAL = "approval"
    BRANCH = "branch"
    DELEGATION = "delegation"
    HANDOFF = "handoff"


class Checkpoint(BaseModel):
    """Immutable provider-independent resume snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    user_id: UUID
    event_sequence: int = Field(ge=1, strict=True)
    kind: CheckpointKind
    agent_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    lease: CapabilityLease
    permission_mode: PermissionMode = PermissionMode.REQUEST
    budget: BudgetSnapshot
    messages: list[JsonValue]
    pending_approval_ids: tuple[UUID, ...] = ()
    pending_delegation_ids: tuple[UUID, ...] = ()
    child_run_ids: tuple[UUID, ...] = ()
    workflow_state: dict[str, JsonValue] | None = None
    temporary_approved_tools: frozenset[str] = frozenset()
    artifact_ids: tuple[UUID, ...] = ()
    run_status: RunStatus
    parent_run_id: UUID | None = None
    depth: int = Field(default=0, ge=0, strict=True)
    source_run_id: UUID | None = None
    source_sequence: int | None = Field(default=None, ge=1, strict=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)
