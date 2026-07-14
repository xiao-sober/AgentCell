"""Durable multi-Agent delegation and handoff domain contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentcell.budgets import Budget, Usage
from agentcell.policy import CapabilityLease


class DelegationKind(StrEnum):
    AGENT_TOOL = "agent_tool"
    HANDOFF = "handoff"


class DelegationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DelegationStatus.COMPLETED,
            DelegationStatus.FAILED,
            DelegationStatus.CANCELLED,
        }


class DelegationRequest(BaseModel):
    """Strict model-facing request for one child Agent execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    task: str = Field(min_length=1, max_length=32_000)
    lease: CapabilityLease
    budget: Budget


class DelegationResult(BaseModel):
    """Bounded structured result returned to a parent Agent or workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delegation_id: UUID
    child_run_id: UUID
    agent_id: str
    status: DelegationStatus
    output: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    usage: Usage = Field(default_factory=Usage)
    approval_ids: tuple[UUID, ...] = ()


class AgentDelegation(BaseModel):
    """Persisted projection linking one parent tool/stage to one child Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    trace_id: UUID = Field(default_factory=uuid4)
    parent_run_id: UUID
    child_run_id: UUID
    provider_call_id: str = Field(min_length=1)
    kind: DelegationKind
    target_agent_id: str = Field(min_length=1)
    task: str = Field(min_length=1)
    depth: int = Field(ge=1, strict=True)
    lease: CapabilityLease
    allocated_budget: Budget
    accounted_usage: Usage = Field(default_factory=Usage)
    status: DelegationStatus = DelegationStatus.PENDING
    result: DelegationResult | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("delegation timestamps must be timezone-aware")
        return value.astimezone(UTC)


class HandoffStage(StrEnum):
    COORDINATOR = "coordinator"
    CODER = "coder"
    REVIEWER = "reviewer"
    FINALIZER = "finalizer"


class HandoffRequest(BaseModel):
    """Application-owned deterministic handoff request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    user_id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID = Field(default_factory=uuid4)
    lease: CapabilityLease
    budget: Budget
    stage_budgets: dict[HandoffStage, Budget]
    stage_leases: dict[HandoffStage, CapabilityLease]


class HandoffResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root_run_id: UUID
    status: DelegationStatus
    stages: tuple[DelegationResult, ...]
    output: str | None = None
