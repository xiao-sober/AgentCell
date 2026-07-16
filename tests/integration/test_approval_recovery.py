"""Stage 6 persisted approval pause and process-restart recovery tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from agentcell.agents import AgentRegistry, AgentSpec
from agentcell.errors import (
    ApprovalConflictError,
    RunIdentityMismatchError,
    ToolArgumentsError,
)
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
from agentcell.storage import ApprovalRepository, CheckpointRepository, Database, EventStore
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


@dataclass
class SuccessfulTestCounter:
    values: list[int]

    async def handle(self, params: ActionParams, context: ToolExecutionContext) -> JsonValue:
        del context
        self.values.append(params.value)
        collected_only = params.value == 1
        return {
            "command": [
                "pytest",
                "tests/",
                *(["--collect-only"] if collected_only else []),
            ],
            "cwd": ".",
            "exit_code": 0,
            "stdout": "48 tests collected" if collected_only else "44 passed, 4 skipped",
            "stderr": "",
            "output_bytes": 20,
            "test_execution": {
                "framework": "pytest",
                "executed": not collected_only,
                "successful": not collected_only,
                "collected_only": collected_only,
                "summary": None if collected_only else "44 passed, 4 skipped",
            },
        }


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


def _successful_test_service(
    database: Database,
    script: FakeScript,
    counter: SuccessfulTestCounter,
) -> tuple[RunService, ProviderFactory]:
    model = FakeModelSpec(model="successful-test-script")
    providers = ProviderFactory(
        {"fake_successful_test": model},
        adapters=(FakeProviderAdapter({model.model: script}),),
    )
    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            name="shell.test",
            description="Run the requested test suite.",
            params_model=ActionParams,
            policy=ToolPolicy(
                risk=RiskLevel.DANGEROUS,
                requires_approval=True,
                idempotent=False,
                timeout_seconds=5,
                max_output_bytes=1_024,
                capabilities=frozenset({Capability.SHELL_EXECUTE}),
            ),
            handler=counter.handle,
        )
    )
    agent = AgentSpec(
        id="coder",
        name="Coder",
        description="Test repair Agent.",
        model_ref="fake_successful_test",
        instructions="Run the full test suite before inspecting anything else.",
        tools=("shell.test",),
        capabilities=frozenset({Capability.SHELL_EXECUTE}),
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
async def test_successful_test_forces_final_answer_after_approval_restart(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = SuccessfulTestCounter([])
    first, first_providers = _successful_test_service(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="shell.test",
                    arguments={"value": 2},
                    tool_call_id="approved-test",
                ),
            )
        ),
        counter,
    )
    try:
        waiting = await first.run(
            RunRequest(
                prompt="fix the failing tests",
                workspace=tmp_path,
                agent_id="coder",
                lease=CapabilityLease(commands=frozenset({"pytest"})),
                finalize_after_successful_test=True,
            )
        )
    finally:
        await first_providers.aclose()

    async with database.session() as session:
        checkpoint = await CheckpointRepository(session).latest(waiting.run.id)
    assert checkpoint.finalize_after_successful_test is True

    restarted, restarted_providers = _successful_test_service(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="shell.test",
                    arguments={"value": 3},
                    tool_call_id="must-be-hidden",
                ),
                FakeTextStep(text="No repair was required; the full test suite passed."),
            )
        ),
        counter,
    )
    try:
        completed = await restarted.resume(
            waiting.approvals[0].id,
            ApprovalDecision(kind=ApprovalDecisionKind.APPROVE),
        )
    finally:
        await restarted_providers.aclose()

    assert completed.run.status.value == "completed"
    assert completed.output == "No repair was required; the full test suite passed."
    assert completed.budget.used.requests == 3
    assert completed.budget.used.tool_calls == 1
    assert counter.values == [2]


@pytest.mark.asyncio
async def test_collect_only_does_not_force_final_answer_before_real_test_run(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = SuccessfulTestCounter([])
    first, first_providers = _successful_test_service(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="shell.test",
                    arguments={"value": 1},
                    tool_call_id="collect-only",
                ),
            )
        ),
        counter,
    )
    try:
        waiting_for_collection = await first.run(
            RunRequest(
                prompt="fix the failing tests",
                workspace=tmp_path,
                agent_id="coder",
                lease=CapabilityLease(commands=frozenset({"pytest"})),
                finalize_after_successful_test=True,
            )
        )
    finally:
        await first_providers.aclose()

    after_collection, after_collection_providers = _successful_test_service(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="shell.test",
                    arguments={"value": 2},
                    tool_call_id="real-test-run",
                ),
            )
        ),
        counter,
    )
    try:
        waiting_for_test = await after_collection.resume(
            waiting_for_collection.approvals[0].id,
            ApprovalDecision(kind=ApprovalDecisionKind.APPROVE),
        )
    finally:
        await after_collection_providers.aclose()

    assert waiting_for_test.run.status.value == "waiting_approval"
    assert counter.values == [1]

    after_test, after_test_providers = _successful_test_service(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="shell.test",
                    arguments={"value": 3},
                    tool_call_id="must-be-hidden-after-real-test",
                ),
                FakeTextStep(text="The real test suite passed; no repair was required."),
            )
        ),
        counter,
    )
    try:
        completed = await after_test.resume(
            waiting_for_test.approvals[0].id,
            ApprovalDecision(kind=ApprovalDecisionKind.APPROVE),
        )
    finally:
        await after_test_providers.aclose()

    assert completed.run.status.value == "completed"
    assert completed.output == "The real test suite passed; no repair was required."
    assert completed.budget.used.requests == 4
    assert completed.budget.used.tool_calls == 2
    assert counter.values == [1, 2]


@pytest.mark.asyncio
async def test_resume_uses_persisted_agent_model_when_registry_order_changes(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = ActionCounter([])
    waiting = await _pause(database, tmp_path, counter)
    original_model = FakeModelSpec(model="approval-script")
    alternate_model = FakeModelSpec(model="alternate-script")
    providers = ProviderFactory(
        {
            "alternate": alternate_model,
            "fake_approval": original_model,
        },
        adapters=(
            FakeProviderAdapter(
                {
                    original_model.model: FakeScript(
                        steps=(FakeTextStep(text="original model resumed"),)
                    ),
                    alternate_model.model: FakeScript(steps=(FakeTextStep(text="wrong model"),)),
                }
            ),
        ),
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
                idempotent=False,
                timeout_seconds=5,
                max_output_bytes=1_024,
                capabilities=frozenset({Capability.FILESYSTEM_READ}),
            ),
            handler=counter.handle,
        )
    )
    changed_registry_spec = AgentSpec(
        id="coordinator",
        name="Changed Coordinator",
        description="Registry changed after the Run paused.",
        model_ref="alternate",
        instructions="Use the alternate model.",
        tools=("test.action",),
        capabilities=frozenset({Capability.FILESYSTEM_READ}),
    )
    service = RunService(
        database=database,
        providers=providers,
        agents=AgentRegistry((changed_registry_spec,)),
        tools=tools,
    )
    try:
        result = await service.resume(
            waiting.approvals[0].id,
            ApprovalDecision(kind=ApprovalDecisionKind.APPROVE),
        )
    finally:
        await providers.aclose()

    assert result.output == "original model resumed"
    assert counter.values == [1]


@pytest.mark.asyncio
async def test_model_snapshot_mismatch_fails_before_approval_is_resolved(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = ActionCounter([])
    waiting = await _pause(database, tmp_path, counter)
    changed_model = FakeModelSpec(model="changed-model")
    providers = ProviderFactory(
        {"fake_approval": changed_model},
        adapters=(
            FakeProviderAdapter(
                {changed_model.model: FakeScript(steps=(FakeTextStep(text="wrong model"),))}
            ),
        ),
    )
    original_agent = waiting.run.execution_identity
    assert original_agent is not None
    service = RunService(
        database=database,
        providers=providers,
        agents=AgentRegistry((original_agent.agent_spec,)),
        tools=ToolRegistry(),
    )
    try:
        with pytest.raises(RunIdentityMismatchError):
            await service.resume(
                waiting.approvals[0].id,
                ApprovalDecision(kind=ApprovalDecisionKind.APPROVE),
            )
        stored = await service.get(waiting.run.id)
        async with database.session() as session:
            approval = await ApprovalRepository(session).get_required(waiting.approvals[0].id)
    finally:
        await providers.aclose()

    assert stored is not None
    assert stored.status.value == "waiting_approval"
    assert approval.status.value == "pending"


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
