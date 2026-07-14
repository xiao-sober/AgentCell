"""Persistable approval requests and explicit user decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentcell.budgets import BudgetSnapshot
from agentcell.events import ArtifactReference, JsonValue, redact_sensitive_data
from agentcell.policy.models import RiskLevel


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalDecisionKind(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    MODIFY = "modify"


class ApprovalDecisionSource(StrEnum):
    HUMAN = "human"
    POLICY_AUTO = "policy-auto"
    POLICY_FULL = "policy-full"


class Approval(BaseModel):
    """Full impact envelope shown to a user before guarded execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    provider_call_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]
    approved_arguments: dict[str, JsonValue] | None = None
    risk: RiskLevel
    impact: str = Field(min_length=1)
    diff: str | None = None
    diff_artifact: ArtifactReference | None = None
    remaining_budget: BudgetSnapshot
    idempotent: bool
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    status: ApprovalStatus = ApprovalStatus.PENDING
    grant_same_tool: bool = False
    decision_message: str | None = None
    decision_source: ApprovalDecisionSource | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None

    @field_validator("arguments", "approved_arguments")
    @classmethod
    def redact_arguments(
        cls,
        value: dict[str, JsonValue] | None,
    ) -> dict[str, JsonValue] | None:
        if value is None:
            return None
        return cast(dict[str, JsonValue], redact_sensitive_data(value))

    @field_validator("created_at", "decided_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approval timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_decision_state(self) -> Approval:
        if self.status is ApprovalStatus.PENDING and self.decided_at is not None:
            raise ValueError("pending approval cannot have decided_at")
        if self.status is not ApprovalStatus.PENDING and self.decided_at is None:
            raise ValueError("resolved approval requires decided_at")
        return self


class ApprovalDecision(BaseModel):
    """One idempotency-comparable approval decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ApprovalDecisionKind
    arguments: dict[str, JsonValue] | None = None
    grant_same_tool: bool = False
    message: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def validate_kind(self) -> ApprovalDecision:
        if self.kind is ApprovalDecisionKind.MODIFY and self.arguments is None:
            raise ValueError("modified approval requires arguments")
        if self.kind is not ApprovalDecisionKind.MODIFY and self.arguments is not None:
            raise ValueError("arguments are only valid for modified approval")
        if self.kind is ApprovalDecisionKind.REJECT and self.grant_same_tool:
            raise ValueError("rejection cannot grant same-tool approval")
        return self
