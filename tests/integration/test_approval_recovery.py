"""Stage 6 persisted approval pause and process-restart recovery tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from agentcell.agents import AgentRegistry, AgentSpec
from agentcell.errors import ApprovalConflictError, ToolArgumentsError
from agentcell.events import EventType, GenericEventPayload, JsonValue
from agentcell.kernel.run_service import RunRequest, RunResult, RunService
from agentcell.policy import (
    ApprovalDecision,
    ApprovalDecisionKind,
    Capability,
    CapabilityLease,
    PermissionMode,
    RiskLevel,
    ToolPolicy,
)
from agentcell.providers import (
    FakeModelSpec,
    FakeProviderAdapter,
    FakeScript,
    FakeTextStep,
    FakeToolCallStep,
    ProviderFactory,
)
from agentcell.storage import ApprovalRepository, Database, EventStore
from agentcell.tools import ToolDefinition, ToolExecutionContext, ToolRegistry


class ActionParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: int = Field(ge=1, strict=True)


@dataclass
class ActionCounter:
    values: list[int]

    async def handle(self, params: ActionParams, context: ToolExecutionContext) -> JsonValue:
        del context
        self.values.append(params.value)
        return {"accepted": params.value}


def _service(
    database: Database,
    script: FakeScript,
    counter: ActionCounter,
    *,
    idempotent: bool = False,
) -> tuple[RunService, ProviderFactory]:
    model = FakeModelSpec(model="approval-script")
    providers = ProviderFactory(
        {"fake_approval": model},
        adapters=(FakeProviderAdapter({model.model: script}),),
    )
    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            name="test.action",
            description="Apply one observable test action.",
            params_model=ActionParams,
            policy=ToolPolicy(
                risk=RiskLevel.GUARDED,
                requires_approval=True,
                idempotent=idempotent,
                timeout_seconds=5,
                max_output_bytes=1_024,
                capabilities=frozenset({Capability.FILESYSTEM_READ}),
            ),
            handler=counter.handle,
        )
    )
    agent = AgentSpec(
        id="coordinator",
        name="Coordinator",
        description="Approval test Agent.",
        model_ref="fake_approval",
        instructions="Use the test action.",
        tools=("test.action",),
        capabilities=frozenset({Capability.FILESYSTEM_READ}),
    )
    return (
        RunService(
            database=database,
            providers=providers,
            agents=AgentRegistry((agent,)),
            tools=tools,
        ),
        providers,
    )


async def _pause(
    database: Database,
    tmp_path: Path,
    counter: ActionCounter,
    *,
    value: int = 1,
    call_id: str = "approval-call-1",
) -> RunResult:
    service, providers = _service(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="test.action",
                    arguments={"value": value},
                    tool_call_id=call_id,
                ),
            )
        ),
        counter,
    )
    try:
        return await service.run(
            RunRequest(
                prompt="perform action",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
            )
        )
    finally:
        await providers.aclose()


@pytest.mark.asyncio
async def test_auto_permission_mode_approves_guarded_tool_with_audit_event(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = ActionCounter([])
    service, providers = _service(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="test.action",
                    arguments={"value": 7},
                    tool_call_id="auto-approval-1",
                ),
                FakeTextStep(text="done"),
            )
        ),
        counter,
    )
    try:
        result = await service.run(
            RunRequest(
                prompt="perform action",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
                permission_mode=PermissionMode.AUTO,
            )
        )
    finally:
        await providers.aclose()

    assert result.run.status.value == "completed"
    assert result.approvals == ()
    assert counter.values == [7]
    async with database.session() as session:
        events = await EventStore(session).list_for_run(result.run.id)
        approvals = await ApprovalRepository(session).list_for_run(result.run.id)
    approved = [event for event in events if event.event_type is EventType.TOOL_APPROVED]
    assert len(approved) == 1
    payload = approved[0].payload
    assert isinstance(payload, GenericEventPayload)
    assert payload.data["decision_source"] == "policy-auto"
    assert len(approvals) == 1
    assert approvals[0].status.value == "approved"
    assert approvals[0].decision_source is not None
    assert approvals[0].decision_source.value == "policy-auto"


@pytest.mark.asyncio
async def test_approval_survives_service_restart_and_resume_is_idempotent(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = ActionCounter([])
    first, first_providers = _service(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="test.action",
                    arguments={"value": 1},
                    tool_call_id="approval-call-1",
                ),
            )
        ),
        counter,
    )
    try:
        waiting = await first.run(
            RunRequest(
                prompt="perform action",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
            )
        )
    finally:
        await first_providers.aclose()

    assert waiting.run.status.value == "waiting_approval"
    assert waiting.output is None
    assert waiting.budget.used.tool_calls == 0
    assert counter.values == []
    approval = waiting.approvals[0]
    assert approval.provider_call_id == "approval-call-1"
    assert approval.arguments == {"value": 1}
    assert approval.impact == "Apply one observable test action."
    assert approval.idempotent is False

    restarted, restarted_providers = _service(
        database,
        FakeScript(steps=(FakeTextStep(text="action complete"),)),
        counter,
    )
    decision = ApprovalDecision(kind=ApprovalDecisionKind.APPROVE)
    try:
        completed = await restarted.resume(approval.id, decision)
        repeated = await restarted.resume(approval.id, decision)
    finally:
        await restarted_providers.aclose()

    assert completed.run.status.value == "completed"
    assert completed.output == "action complete"
    assert counter.values == [1]
    assert repeated.run.status.value == "completed"
    assert repeated.output is None
    assert counter.values == [1]

    async with database.session() as session:
        events = await EventStore(session).list_for_run(completed.run.id)
    event_types = [event.event_type for event in events]
    assert EventType.TOOL_APPROVAL_REQUIRED in event_types
    assert EventType.CHECKPOINT_CREATED in event_types
    assert EventType.TOOL_APPROVED in event_types
    assert event_types[-1] is EventType.RUN_COMPLETED


@pytest.mark.asyncio
async def test_rejected_approval_resumes_with_denial_without_execution(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = ActionCounter([])
    waiting = await _pause(database, tmp_path, counter)
    restarted, providers = _service(
        database,
        FakeScript(steps=(FakeTextStep(text="denial handled"),)),
        counter,
    )
    try:
        result = await restarted.resume(
            waiting.approvals[0].id,
            ApprovalDecision(
                kind=ApprovalDecisionKind.REJECT,
                message="Not authorized for this Run.",
            ),
        )
    finally:
        await providers.aclose()

    assert result.output == "denial handled"
    assert result.run.status.value == "completed"
    assert counter.values == []


@pytest.mark.asyncio
async def test_modified_arguments_are_validated_and_executed(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = ActionCounter([])
    waiting = await _pause(database, tmp_path, counter)
    restarted, providers = _service(
        database,
        FakeScript(steps=(FakeTextStep(text="modified action complete"),)),
        counter,
    )
    try:
        result = await restarted.resume(
            waiting.approvals[0].id,
            ApprovalDecision(
                kind=ApprovalDecisionKind.MODIFY,
                arguments={"value": 7},
            ),
        )
    finally:
        await providers.aclose()

    assert result.output == "modified action complete"
    assert counter.values == [7]


@pytest.mark.asyncio
async def test_temporary_same_tool_approval_only_applies_to_current_run(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = ActionCounter([])
    waiting = await _pause(database, tmp_path, counter)
    restarted, providers = _service(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="test.action",
                    arguments={"value": 2},
                    tool_call_id="approval-call-2",
                ),
                FakeTextStep(text="both complete"),
            )
        ),
        counter,
    )
    try:
        result = await restarted.resume(
            waiting.approvals[0].id,
            ApprovalDecision(
                kind=ApprovalDecisionKind.APPROVE,
                grant_same_tool=True,
            ),
        )
    finally:
        await providers.aclose()

    assert result.output == "both complete"
    assert result.approvals == ()
    assert counter.values == [1, 2]


@pytest.mark.asyncio
async def test_invalid_modified_arguments_leave_run_waiting(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = ActionCounter([])
    waiting = await _pause(database, tmp_path, counter)
    restarted, providers = _service(
        database,
        FakeScript(steps=(FakeTextStep(text="unused"),)),
        counter,
    )
    try:
        with pytest.raises(ToolArgumentsError):
            await restarted.resume(
                waiting.approvals[0].id,
                ApprovalDecision(
                    kind=ApprovalDecisionKind.MODIFY,
                    arguments={"value": 0},
                ),
            )
        stored = await restarted.get(waiting.run.id)
    finally:
        await providers.aclose()

    assert stored is not None
    assert stored.status.value == "waiting_approval"
    assert counter.values == []


@pytest.mark.asyncio
async def test_waiting_run_cancel_is_idempotent(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = ActionCounter([])
    waiting = await _pause(database, tmp_path, counter)
    service, providers = _service(
        database,
        FakeScript(steps=(FakeTextStep(text="unused"),)),
        counter,
    )
    try:
        cancelled = await service.cancel(waiting.run.id)
        repeated = await service.cancel(waiting.run.id)
    finally:
        await providers.aclose()

    assert cancelled.status.value == "cancelled"
    assert repeated == cancelled
    async with database.session() as session:
        events = await EventStore(session).list_for_run(waiting.run.id)
    assert [event.event_type for event in events].count(EventType.RUN_CANCELLED) == 1


@pytest.mark.asyncio
async def test_conflicting_repeated_decision_is_rejected(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = ActionCounter([])
    waiting = await _pause(database, tmp_path, counter)
    service, providers = _service(
        database,
        FakeScript(steps=(FakeTextStep(text="complete"),)),
        counter,
    )
    try:
        await service.resume(
            waiting.approvals[0].id,
            ApprovalDecision(kind=ApprovalDecisionKind.APPROVE),
        )
        with pytest.raises(ApprovalConflictError):
            await service.resume(
                waiting.approvals[0].id,
                ApprovalDecision(kind=ApprovalDecisionKind.REJECT),
            )
    finally:
        await providers.aclose()
