"""Versioned transport-neutral task routing contracts."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from agentcell.budgets import Budget, BudgetSnapshot, Usage
from agentcell.events import JsonValue
from agentcell.kernel.models import Run
from agentcell.policy import Approval, Capability, CapabilityLease, PermissionMode


class TaskRouteMode(StrEnum):
    SINGLE_AGENT = "single_agent"
    TEAM = "team"


class TaskRouteSource(StrEnum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"
    OVERRIDE = "override"
    SAFE_FALLBACK = "safe_fallback"


class TaskRouteStatus(StrEnum):
    READY = "ready"
    CONFIRMATION_REQUIRED = "confirmation_required"
    REJECTED = "rejected"


class RouteBudgetProfile(StrEnum):
    READ_ONLY = "read_only"
    CHANGE = "change"
    REVIEW = "review"
    RESEARCH = "research"
    DELIVERY = "delivery"


class TaskRouteIssueCode(StrEnum):
    WORKSPACE_INVALID = "workspace_invalid"
    TARGET_UNAVAILABLE = "target_unavailable"
    TARGET_NOT_PUBLIC = "target_not_public"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    BUDGET_INSUFFICIENT = "budget_insufficient"
    CAPABILITY_MISSING = "capability_missing"
    CLASSIFICATION_AMBIGUOUS = "classification_ambiguous"
    MODEL_FALLBACK_FAILED = "model_fallback_failed"


class ModelRouteClassification(BaseModel):
    """Only the bounded public label set may cross the routing model boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: Literal["coordinator", "coder", "reviewer", "researcher", "software"]
    confidence: float = Field(ge=0, le=1)
    reason_summary: str = Field(min_length=1, max_length=300)
    requires_clarification: bool = False


class TaskRouteIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: TaskRouteIssueCode
    message: str = Field(min_length=1, max_length=500)
    capability: Capability | None = None


class RoutingPolicy(BaseModel):
    """Versioned public target allowlist and deterministic confidence boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, strict=True)
    policy_version: str = Field(default="9.4.1-v1", min_length=1, max_length=64)
    public_agent_ids: frozenset[str] = frozenset({"coordinator", "coder", "reviewer", "researcher"})
    public_team_ids: frozenset[str] = frozenset({"software"})
    automatic_confidence_threshold: float = Field(default=0.8, ge=0, le=1)
    model_fallback_enabled: bool = True
    model_max_output_tokens: int = Field(default=2_048, ge=128, le=8_192, strict=True)
    model_timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    @field_serializer("public_agent_ids", "public_team_ids", when_used="json")
    def serialize_ids(self, value: frozenset[str]) -> list[str]:
        return sorted(value)


class TaskRouteRequest(BaseModel):
    """All authority supplied to routing; routing may only select within these bounds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str = Field(min_length=1, max_length=32_000)
    workspace: Path
    lease: CapabilityLease
    permission_mode: PermissionMode = PermissionMode.REQUEST
    budget: Budget
    user_id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID = Field(default_factory=uuid4)
    root_run_id: UUID = Field(default_factory=uuid4)
    project_id: str | None = Field(default=None, min_length=1, max_length=512)
    model_ref: str | None = Field(default=None, min_length=1, max_length=255)
    agent_id: str | None = Field(
        default=None,
        min_length=1,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )
    team_id: str | None = Field(
        default=None,
        min_length=1,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )

    @model_validator(mode="after")
    def validate_override(self) -> Self:
        if self.agent_id is not None and self.team_id is not None:
            raise ValueError("agent_id and team_id overrides are mutually exclusive")
        return self

    @property
    def task_sha256(self) -> str:
        return hashlib.sha256(self.task.encode("utf-8")).hexdigest()


class TaskExecutionResult(BaseModel):
    """Transport-neutral outcome of an authoritative routed task root."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run: Run
    decision: TaskRouteDecision
    output: str | None = None
    budget: BudgetSnapshot
    child_run_ids: tuple[UUID, ...] = ()
    approvals: tuple[Approval, ...] = ()


class TaskRouteDecision(BaseModel):
    """Safe, reproducible route selection and validation result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, strict=True)
    policy_version: str = Field(min_length=1, max_length=64)
    mode: TaskRouteMode
    agent_id: str | None = Field(default=None, min_length=1)
    team_id: str | None = Field(default=None, min_length=1)
    source: TaskRouteSource
    status: TaskRouteStatus
    confidence: float = Field(ge=0, le=1)
    reason_summary: str = Field(min_length=1, max_length=500)
    required_capabilities: frozenset[Capability] = frozenset()
    capability_gaps: frozenset[Capability] = frozenset()
    budget_profile: RouteBudgetProfile
    requires_confirmation: bool = False
    issues: tuple[TaskRouteIssue, ...] = ()
    routing_usage: Usage = Field(default_factory=Usage)
    decision_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_serializer("required_capabilities", "capability_gaps", when_used="json")
    def serialize_capabilities(self, value: frozenset[Capability]) -> list[str]:
        return sorted(item.value for item in value)

    @model_validator(mode="after")
    def validate_target_status_and_hash(self) -> Self:
        has_agent = self.agent_id is not None
        has_team = self.team_id is not None
        if has_agent == has_team:
            raise ValueError("route decision must identify exactly one Agent or Team")
        if self.mode is TaskRouteMode.SINGLE_AGENT and not has_agent:
            raise ValueError("single_agent route requires agent_id")
        if self.mode is TaskRouteMode.TEAM and not has_team:
            raise ValueError("team route requires team_id")
        expected_confirmation = self.status is TaskRouteStatus.CONFIRMATION_REQUIRED
        if self.requires_confirmation is not expected_confirmation:
            raise ValueError("requires_confirmation must match route status")
        if not self.capability_gaps.issubset(self.required_capabilities):
            raise ValueError("capability gaps must be a subset of required capabilities")
        expected_hash = self._compute_hash()
        if self.decision_hash is not None and self.decision_hash != expected_hash:
            raise ValueError("decision_hash does not match the route decision")
        object.__setattr__(self, "decision_hash", expected_hash)
        return self

    @property
    def target_id(self) -> str:
        return self.agent_id or cast(str, self.team_id)

    def _compute_hash(self) -> str:
        raw = cast(
            dict[str, JsonValue],
            self.model_dump(
                mode="json",
                exclude={"decision_hash", "routing_usage"},
            ),
        )
        encoded = json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
