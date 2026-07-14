"""Run-bound persistence for deterministic policy approvals."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from agentcell.budgets import BudgetTracker
from agentcell.events import EventType, GenericEventPayload
from agentcell.policy import (
    Approval,
    ApprovalDecisionSource,
    ApprovalStatus,
    ToolPolicy,
)
from agentcell.storage import ApprovalRepository, Database
from agentcell.tools import ToolApprovalPreview, ToolCall, ToolExecutionContext


class RunApprovalRecorder:
    """Persist policy-auto/full decisions with the same envelope as human approvals."""

    def __init__(
        self,
        database: Database,
        *,
        run_id: UUID,
        agent_id: str,
        agent_name: str,
        provider: str,
        model: str,
        budget: BudgetTracker,
    ) -> None:
        self._database = database
        self._run_id = run_id
        self._agent_id = agent_id
        self._agent_name = agent_name
        self._provider = provider
        self._model = model
        self._budget = budget

    async def record(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
        *,
        policy: ToolPolicy,
        preview: ToolApprovalPreview | None,
        source: str,
    ) -> UUID:
        if call.provider_call_id is None:
            raise ValueError("Automatic approval requires a Provider tool-call id")
        decision_source = ApprovalDecisionSource(source)
        async with self._database.transaction() as session:
            repository = ApprovalRepository(session)
            existing = await repository.find_by_provider_call(self._run_id, call.provider_call_id)
            if existing is None:
                approval = Approval(
                    run_id=self._run_id,
                    provider_call_id=call.provider_call_id,
                    agent_id=self._agent_id,
                    agent_name=self._agent_name,
                    provider=self._provider,
                    model=self._model,
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                    approved_arguments=call.arguments,
                    risk=policy.risk,
                    impact=(
                        call.tool_name
                        if preview is None or preview.impact is None
                        else preview.impact
                    ),
                    diff=None if preview is None else preview.diff,
                    diff_artifact=None if preview is None else preview.diff_artifact,
                    remaining_budget=self._budget.snapshot(),
                    idempotent=policy.idempotent,
                    timeout_seconds=policy.timeout_seconds,
                    status=ApprovalStatus.APPROVED,
                    decision_message="Approved by deterministic Run permission policy",
                    decision_source=decision_source,
                    decided_at=datetime.now(UTC),
                )
                await repository.create(approval)
            else:
                approval = existing
        await context.events.emit(
            EventType.TOOL_APPROVED,
            GenericEventPayload(
                data={
                    "approval_id": str(approval.id),
                    "provider_call_id": call.provider_call_id,
                    "tool_name": call.tool_name,
                    "risk": policy.risk.value,
                    "decision_source": decision_source.value,
                }
            ),
        )
        return approval.id
