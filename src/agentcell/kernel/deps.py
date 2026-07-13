"""Run-scoped dependencies injected into PydanticAI and tool handlers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from agentcell.agents import AgentRegistry
from agentcell.budgets import BudgetTracker
from agentcell.memory.service import MemoryService
from agentcell.policy import CapabilityLease
from agentcell.tools import (
    ArtifactStore,
    ToolEventSink,
    ToolExecutionContext,
    ToolExecutionLedger,
    ToolExecutor,
)


@dataclass(frozen=True, slots=True)
class RunDeps:
    """Explicit mutable-service references for one Run, never stored on AgentSpec."""

    run_id: UUID
    conversation_id: UUID
    user_id: UUID
    workspace: Path
    lease: CapabilityLease
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
    artifacts: ArtifactStore | None = None
    memory: MemoryService | None = None

    def tool_context(self) -> ToolExecutionContext:
        return ToolExecutionContext(
            workspace=self.workspace,
            lease=self.lease,
            budget=self.budget,
            events=self.events,
            ledger=self.ledger,
            artifacts=self.artifacts,
        )
