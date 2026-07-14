"""Stable transport DTOs that do not expose ORM or secret-bearing configuration."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from agentcell.agents import AgentSpec
from agentcell.budgets import Budget
from agentcell.conversations import Conversation, ConversationMessage, ConversationMessageKind
from agentcell.events import JsonValue
from agentcell.kernel.lifecycle import RunStatus
from agentcell.kernel.models import Run
from agentcell.memory import MemoryItem
from agentcell.policy import (
    ApprovalDecision,
    ApprovalDecisionSource,
    ApprovalStatus,
    CapabilityLease,
    PermissionMode,
    RiskLevel,
)


def _default_coordinator_lease() -> CapabilityLease:
    """Grant only read access and bounded delegation to the default coordinator."""

    return CapabilityLease(
        filesystem_read=(".",),
        can_delegate=True,
        max_child_depth=2,
    )


class ProblemDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    title: str
    status: int
    detail: str
    code: str
    instance: str | None = None
    retryable: bool = False


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1, max_length=64_000)
    workspace: Path
    agent_id: str = "coordinator"
    conversation_id: UUID = Field(default_factory=uuid4)
    user_id: UUID = Field(default_factory=uuid4)
    run_id: UUID = Field(default_factory=uuid4)
    lease: CapabilityLease = Field(default_factory=_default_coordinator_lease)
    permission_mode: PermissionMode = PermissionMode.REQUEST
    budget: Budget | None = None


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    conversation_id: UUID
    agent_id: str
    parent_run_id: UUID | None
    status: RunStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, run: Run) -> RunResponse:
        return cls.model_validate(run.model_dump())


class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: UUID
    workspace: Path
    agent_id: str = "coordinator"
    project_id: str | None = Field(default=None, min_length=1, max_length=512)
    title: str | None = Field(default=None, max_length=255)
    conversation_id: UUID = Field(default_factory=uuid4)


class ConversationTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1, max_length=64_000)
    user_id: UUID
    run_id: UUID = Field(default_factory=uuid4)
    lease: CapabilityLease = Field(default_factory=_default_coordinator_lease)
    permission_mode: PermissionMode = PermissionMode.REQUEST
    budget: Budget | None = None


class ConversationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    user_id: UUID
    project_id: str
    workspace: str
    agent_id: str
    title: str | None
    active_run_id: UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: Conversation) -> ConversationResponse:
        return cls.model_validate(value.model_dump())


class ConversationMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    conversation_id: UUID
    run_id: UUID
    sequence: int
    kind: ConversationMessageKind
    payload_version: int
    payload: dict[str, JsonValue]
    artifact_ids: tuple[UUID, ...]
    created_at: datetime

    @classmethod
    def from_domain(cls, value: ConversationMessage) -> ConversationMessageResponse:
        return cls.model_validate(value.model_dump())


class ResumeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: UUID | None = None
    decision: ApprovalDecision | None = None


class BranchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    from_sequence: int = Field(ge=1, strict=True)


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    run_id: UUID
    provider_call_id: str
    agent_id: str
    agent_name: str
    provider: str
    model: str
    tool_name: str
    arguments: dict[str, JsonValue]
    risk: RiskLevel
    impact: str
    diff: str | None
    status: ApprovalStatus
    decision_source: ApprovalDecisionSource | None
    idempotent: bool
    timeout_seconds: float
    created_at: datetime
    decided_at: datetime | None


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: ApprovalDecision


class ChangeRevertRequest(BaseModel):
    """Explicit human confirmation and lease for one hash-safe reverse change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    confirm: Literal[True]
    lease: CapabilityLease


class AgentWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    spec: AgentSpec


class ToolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    parameters: dict[str, object]
    risk: RiskLevel
    requires_approval: bool
    idempotent: bool
    timeout_seconds: float
    max_output_bytes: int
    capabilities: list[str]


class ProviderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_ref: str
    provider: str
    model: str
    max_output_tokens: int
    temperature: float | None
    timeout_seconds: float
    max_retries: int
    thinking: bool | None = None


class MemorySearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item: MemoryItem
    score: float
    bm25_relevance: float
    time_decay: float
    tag_overlap: float


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "degraded"]
    database: Literal["ok", "unavailable"]


class VersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    api_version: str = "v1"
    event_protocol: str = "ag-ui"
