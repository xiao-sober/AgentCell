"""Run-scoped dependencies injected into PydanticAI and tool handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from agentcell.agents import AgentRegistry
from agentcell.budgets import BudgetTracker
from agentcell.kernel.final_output import FinalOutputState
from agentcell.memory.service import MemoryService
from agentcell.policy import CapabilityLease, PermissionMode
from agentcell.tools import (
    AgentDelegationExecutor,
    ApprovalRecorder,
    ArtifactStore,
    ChangeRecorder,
    ToolEventSink,
    ToolExecutionContext,
    ToolExecutionLedger,
    ToolExecutor,
)


@dataclass(slots=True)
class ToolRetryState:
    """Allow one model correction for a safe, side-effect-free tool mistake."""

    used: bool = False

    def consume(self) -> bool:
        if self.used:
            return False
        self.used = True
        return True


@dataclass(frozen=True, slots=True)
class RunDeps:
    """Explicit mutable-service references for one Run, never stored on AgentSpec."""

    run_id: UUID
    conversation_id: UUID
    user_id: UUID
    workspace: Path
    lease: CapabilityLease
    permission_mode: PermissionMode
    budget: BudgetTracker
    events: ToolEventSink
    tools: ToolExecutor
    agents: AgentRegistry
    agent_id: str
    agent_name: str
    provider: str
    model: str
    temporary_approved_tools: frozenset[str] = frozenset()
    ledger: ToolExecutionLedger | None = None
    changes: ChangeRecorder | None = None
    approvals: ApprovalRecorder | None = None
    artifacts: ArtifactStore | None = None
    memory: MemoryService | None = None
    depth: int = 0
    delegation: AgentDelegationExecutor | None = None
    finalize_after_successful_test: bool = False
    deferred_tool_results_at_request: int | None = None
    tool_retries: ToolRetryState = field(default_factory=ToolRetryState)
    final_output: FinalOutputState = field(default_factory=FinalOutputState)

    def tool_context(self, *, provider_call_id: str | None = None) -> ToolExecutionContext:
        return ToolExecutionContext(
            workspace=self.workspace,
            lease=self.lease,
            budget=self.budget,
            events=self.events,
            ledger=self.ledger,
            changes=self.changes,
            approvals=self.approvals,
            artifacts=self.artifacts,
            run_id=self.run_id,
            conversation_id=self.conversation_id,
            user_id=self.user_id,
            agent_id=self.agent_id,
            depth=self.depth,
            delegation=self.delegation,
            provider_call_id=provider_call_id,
        )
