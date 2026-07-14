"""Structured tool calls, results, definitions, and execution dependency protocols."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from agentcell.budgets import BudgetTracker
from agentcell.events import ArtifactReference, EventPayload, EventType, JsonValue
from agentcell.policy import CapabilityLease, ToolPolicy

if TYPE_CHECKING:
    from agentcell.agents import DelegationRequest, DelegationResult

type ToolHandlerOutput = JsonValue | BaseModel


class ToolCall(BaseModel):
    """One validated-by-boundary request to invoke a registered tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: UUID = Field(default_factory=uuid4)
    provider_call_id: str | None = Field(default=None, min_length=1)
    tool_name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Size-bounded tool output with an optional full Artifact reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: UUID
    tool_name: str
    output: JsonValue
    output_bytes: int = Field(ge=0, strict=True)
    truncated: bool = False
    artifact: ArtifactReference | None = None
    duration_ms: float = Field(ge=0, allow_inf_nan=False)


class ToolEventSink(Protocol):
    """Run-bound event consumer supplied by the orchestration layer."""

    async def emit(self, event_type: EventType, payload: EventPayload) -> None:
        """Record one ordered domain-event intent."""

        ...


class ArtifactStore(Protocol):
    """Store oversized tool output outside the event and model context."""

    async def save(
        self,
        content: bytes,
        *,
        media_type: str,
        suggested_name: str,
    ) -> ArtifactReference:
        """Persist bytes and return a stable content reference."""

        ...


class ToolApprovalPreview(BaseModel):
    """Bounded, persistable impact details computed before user approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    impact: str | None = Field(default=None, min_length=1, max_length=2_000)
    diff: str | None = Field(default=None, max_length=32_000)
    diff_artifact: ArtifactReference | None = None


class ToolExecutionLedger(Protocol):
    """Durable idempotency boundary keyed by Provider tool-call identity."""

    async def begin(self, call: ToolCall, *, idempotent: bool) -> ToolResult | None:
        """Claim an execution or return its previously completed result."""

        ...

    async def complete(self, call: ToolCall, result: ToolResult) -> None:
        """Persist a completed result before the Run advances."""

        ...

    async def fail(self, call: ToolCall) -> None:
        """Persist that a claimed execution failed."""

        ...


class ChangeRecorder(Protocol):
    """Durable before/after recorder for workspace mutation tools."""

    async def prepare(
        self,
        call: ToolCall,
        params: BaseModel,
        context: ToolExecutionContext,
    ) -> UUID | None: ...

    async def complete(self, change_id: UUID, context: ToolExecutionContext) -> None: ...

    async def fail(self, change_id: UUID, context: ToolExecutionContext) -> None: ...


class ApprovalRecorder(Protocol):
    """Persist one deterministic PolicyEngine approval before execution."""

    async def record(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
        *,
        policy: ToolPolicy,
        preview: ToolApprovalPreview | None,
        source: str,
    ) -> UUID: ...


class AgentDelegationExecutor(Protocol):
    """Kernel-owned child execution boundary injected into delegation tools."""

    async def delegate(
        self,
        request: DelegationRequest,
        context: ToolExecutionContext,
        *,
        provider_call_id: str,
    ) -> DelegationResult: ...


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Explicit Run-scoped dependencies available to tool execution."""

    workspace: Path
    lease: CapabilityLease
    budget: BudgetTracker
    events: ToolEventSink
    artifacts: ArtifactStore | None = None
    ledger: ToolExecutionLedger | None = None
    changes: ChangeRecorder | None = None
    approvals: ApprovalRecorder | None = None
    run_id: UUID | None = None
    conversation_id: UUID | None = None
    user_id: UUID | None = None
    agent_id: str | None = None
    depth: int = 0
    delegation: AgentDelegationExecutor | None = None
    provider_call_id: str | None = None


class ToolHandler[ParamsT: BaseModel](Protocol):
    """Async handler receiving validated parameters and Run-scoped dependencies."""

    async def __call__(
        self,
        params: ParamsT,
        context: ToolExecutionContext,
    ) -> ToolHandlerOutput:
        """Execute one tool call."""

        ...


class ToolApprovalPreviewer[ParamsT: BaseModel](Protocol):
    """Compute safe approval details without performing the requested mutation."""

    async def __call__(
        self,
        params: ParamsT,
        context: ToolExecutionContext,
    ) -> ToolApprovalPreview: ...


@dataclass(frozen=True, slots=True)
class ToolDefinition[ParamsT: BaseModel]:
    """Immutable registry entry pairing schema, policy, and implementation."""

    name: str
    description: str
    params_model: type[ParamsT]
    policy: ToolPolicy
    handler: ToolHandler[ParamsT]
    approval_previewer: ToolApprovalPreviewer[ParamsT] | None = None
