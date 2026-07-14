"""Stage 8 Agent-as-Tool budget, authority, events, and persistence tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from agentcell.agents import (
    AgentDelegation,
    AgentRegistry,
    AgentSpec,
    DelegationResult,
    DelegationStatus,
    reviewer_spec,
)
from agentcell.budgets import Budget
from agentcell.events import EventType, JsonValue, RunStatusChangedPayload
from agentcell.kernel.checkpoint import CheckpointKind
from agentcell.kernel.lifecycle import RunStatus
from agentcell.kernel.run_service import RunRequest, RunService
from agentcell.policy import (
    ApprovalDecision,
    ApprovalDecisionKind,
    Capability,
    CapabilityLease,
    RiskLevel,
    ToolPolicy,
)
from agentcell.providers import (
    FakeFailureKind,
    FakeFailureStep,
    FakeModelSpec,
    FakeProviderAdapter,
    FakeScript,
    FakeTextStep,
    FakeToolCallStep,
    ProviderFactory,
)
from agentcell.storage import (
    AgentDelegationRepository,
    CheckpointRepository,
    Database,
    EventStore,
    RunRepository,
)
from agentcell.tools import ToolDefinition, ToolExecutionContext, ToolRegistry


class ChildActionParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SimulatedProcessExit(BaseException):
    """Fault injection that bypasses in-process exception cleanup."""


@dataclass
class ChildAction:
    calls: int = 0

    async def __call__(
        self,
        params: ChildActionParams,
        context: ToolExecutionContext,
    ) -> str:
        del params, context
        self.calls += 1
        return "approved child action"


def _budget(*, requests: int, tools: int, children: int, depth: int) -> Budget:
    return Budget(
        max_requests=requests,
        max_input_tokens=1_000,
        max_output_tokens=1_000,
        max_total_tokens=2_000,
        max_tool_calls=tools,
        max_duration_seconds=60,
        max_cost=None,
        max_children=children,
        max_depth=depth,
    )


def _service(
    database: Database,
    request_arguments: dict[str, JsonValue],
    *,
    child_script: FakeScript | None = None,
) -> tuple[RunService, ProviderFactory]:
    parent_model = FakeModelSpec(model="parent-delegation")
    child_model = FakeModelSpec(model="child-delegation")
    providers = ProviderFactory(
        {"parent": parent_model, "child": child_model},
        adapters=(
            FakeProviderAdapter(
                {
                    parent_model.model: FakeScript(
                        steps=(
                            FakeToolCallStep(
                                tool_name="agent.delegate",
                                arguments=request_arguments,
                                tool_call_id="delegate-1",
                            ),
                            FakeTextStep(text="parent complete"),
                        )
                    ),
                    child_model.model: child_script
                    or FakeScript(steps=(FakeTextStep(text="child complete"),)),
                }
            ),
        ),
    )
    parent = AgentSpec(
        id="coordinator",
        name="Coordinator",
        description="Delegates one test task.",
        model_ref="parent",
        instructions="Delegate the task.",
        tools=("agent.delegate",),
        capabilities=frozenset({Capability.AGENT_DELEGATE}),
        max_children=1,
        max_depth=1,
    )
    child = AgentSpec(
        id="worker",
        name="Worker",
        description="Completes one child task.",
        model_ref="child",
        instructions="Complete the task.",
    )
    service = RunService(
        database=database,
        providers=providers,
        agents=AgentRegistry((parent, child)),
        tools=ToolRegistry(),
    )
    return service, providers


@pytest.mark.asyncio
async def test_agent_as_tool_links_runs_and_rolls_child_usage_into_parent(
    database: Database,
    tmp_path: Path,
) -> None:
    child_budget = _budget(
        requests=1,
        tools=0,
        children=0,
        depth=0,
    ).model_copy(
        update={
            "max_input_tokens": 100,
            "max_output_tokens": 100,
            "max_total_tokens": 200,
            "max_duration_seconds": 30,
        }
    )
    child_lease = CapabilityLease()
    service, providers = _service(
        database,
        {
            "agent_id": "worker",
            "task": "do child work",
            "lease": child_lease.model_dump(mode="json"),
            "budget": child_budget.model_dump(mode="json"),
        },
    )
    try:
        result = await service.run(
            RunRequest(
                prompt="delegate",
                workspace=tmp_path,
                lease=CapabilityLease(can_delegate=True, max_child_depth=1),
                budget=_budget(requests=4, tools=1, children=1, depth=1),
            )
        )
    finally:
        await providers.aclose()

    assert result.output == "parent complete"
    assert result.budget.used.requests == 4
    assert result.budget.used.tool_calls == 1
    assert result.budget.used.children == 1
    async with database.session() as session:
        events = await EventStore(session).list_for_run(result.run.id)
        delegations = await AgentDelegationRepository(session).list_active_for_parent(result.run.id)
    assert delegations == []
    event_types = [event.event_type for event in events]
    assert EventType.AGENT_CHILD_STARTED in event_types
    assert EventType.AGENT_CHILD_COMPLETED in event_types


@pytest.mark.asyncio
async def test_interrupted_child_is_safely_failed_and_parent_resumes_from_checkpoint(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_budget = _budget(requests=1, tools=0, children=0, depth=0).model_copy(
        update={
            "max_input_tokens": 100,
            "max_output_tokens": 100,
            "max_total_tokens": 200,
            "max_duration_seconds": 30,
        }
    )
    arguments = cast(
        dict[str, JsonValue],
        {
            "agent_id": "worker",
            "task": "interrupt child work",
            "lease": CapabilityLease().model_dump(mode="json"),
            "budget": child_budget.model_dump(mode="json"),
        },
    )
    service, providers = _service(database, arguments)
    captured: dict[str, AgentDelegation] = {}

    async def stop_after_checkpoint(
        delegation: AgentDelegation,
        *,
        workspace: Path,
        user_id: UUID,
        conversation_id: UUID,
    ) -> DelegationResult:
        del workspace, user_id, conversation_id
        captured["delegation"] = delegation
        raise SimulatedProcessExit

    monkeypatch.setattr(service, "recover_delegation_child", stop_after_checkpoint)
    parent_run_id = uuid4()
    try:
        with pytest.raises(SimulatedProcessExit):
            await service.run(
                RunRequest(
                    run_id=parent_run_id,
                    prompt="delegate interrupted work",
                    workspace=tmp_path,
                    lease=CapabilityLease(can_delegate=True, max_child_depth=1),
                    budget=_budget(requests=4, tools=1, children=1, depth=1),
                )
            )
    finally:
        await providers.aclose()

    delegation = captured["delegation"]
    async with database.transaction() as session:
        parent = await RunRepository(session).get(parent_run_id)
        child = await RunRepository(session).get(delegation.child_run_id)
        checkpoint = await CheckpointRepository(session).latest(parent_run_id)
        assert parent is not None
        assert child is not None
        assert parent.status is RunStatus.PAUSED
        assert child.status is RunStatus.CREATED
        assert checkpoint.kind is CheckpointKind.DELEGATION
        assert checkpoint.messages
        running = child.transition_to(RunStatus.RUNNING)
        await RunRepository(session).save(running)
        await EventStore(session).append(
            run_id=child.id,
            event_type=EventType.RUN_STATUS_CHANGED,
            payload=RunStatusChangedPayload(
                previous_status=RunStatus.CREATED,
                status=RunStatus.RUNNING,
            ),
        )

    restarted, restarted_providers = _service(database, arguments)
    try:
        completed = await restarted.resume_delegation(parent_run_id)
    finally:
        await restarted_providers.aclose()

    assert completed.run.status is RunStatus.COMPLETED
    assert completed.output == "parent complete"
    async with database.session() as session:
        settled = await AgentDelegationRepository(session).get_required(delegation.id)
        child = await RunRepository(session).get(delegation.child_run_id)
    assert child is not None
    assert child.status is RunStatus.FAILED
    assert settled.result is not None
    assert settled.result.error_code == "child_run_interrupted"


@pytest.mark.asyncio
async def test_terminal_child_result_is_durable_before_parent_settlement(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_budget = _budget(requests=1, tools=0, children=0, depth=0).model_copy(
        update={
            "max_input_tokens": 100,
            "max_output_tokens": 100,
            "max_total_tokens": 200,
            "max_duration_seconds": 30,
        }
    )
    arguments = cast(
        dict[str, JsonValue],
        {
            "agent_id": "worker",
            "task": "complete before crash",
            "lease": CapabilityLease().model_dump(mode="json"),
            "budget": child_budget.model_dump(mode="json"),
        },
    )
    service, providers = _service(database, arguments)
    recover = service.recover_delegation_child
    captured: dict[str, AgentDelegation] = {}

    async def stop_after_child_completion(
        delegation: AgentDelegation,
        *,
        workspace: Path,
        user_id: UUID,
        conversation_id: UUID,
    ) -> DelegationResult:
        await recover(
            delegation,
            workspace=workspace,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        captured["delegation"] = delegation
        raise SimulatedProcessExit

    monkeypatch.setattr(service, "recover_delegation_child", stop_after_child_completion)
    parent_run_id = uuid4()
    try:
        with pytest.raises(SimulatedProcessExit):
            await service.run(
                RunRequest(
                    run_id=parent_run_id,
                    prompt="delegate durable completion",
                    workspace=tmp_path,
                    lease=CapabilityLease(can_delegate=True, max_child_depth=1),
                    budget=_budget(requests=4, tools=1, children=1, depth=1),
                )
            )
    finally:
        await providers.aclose()

    delegation = captured["delegation"]
    async with database.session() as session:
        durable = await AgentDelegationRepository(session).get_required(delegation.id)
        child = await RunRepository(session).get(delegation.child_run_id)
    assert child is not None
    assert child.status is RunStatus.COMPLETED
    assert durable.result is not None
    assert durable.result.status is DelegationStatus.COMPLETED
    assert durable.result.output == "child complete"

    restarted, restarted_providers = _service(database, arguments)
    try:
        completed = await restarted.resume_delegation(parent_run_id)
    finally:
        await restarted_providers.aclose()
    assert completed.run.status is RunStatus.COMPLETED
    assert completed.output == "parent complete"


def test_reviewer_spec_is_structurally_read_only() -> None:
    reviewer = reviewer_spec(model_ref="review-model")

    assert reviewer.capabilities == frozenset({Capability.FILESYSTEM_READ})
    assert set(reviewer.tools) == {
        "workspace.list",
        "workspace.read",
        "workspace.search",
    }
    assert Capability.FILESYSTEM_WRITE not in reviewer.capabilities


@pytest.mark.asyncio
async def test_child_failure_is_structured_for_parent_and_persisted(
    database: Database,
    tmp_path: Path,
) -> None:
    child_budget = _budget(requests=1, tools=0, children=0, depth=0).model_copy(
        update={
            "max_input_tokens": 100,
            "max_output_tokens": 100,
            "max_total_tokens": 200,
            "max_duration_seconds": 30,
        }
    )
    service, providers = _service(
        database,
        {
            "agent_id": "worker",
            "task": "fail safely",
            "lease": CapabilityLease().model_dump(mode="json"),
            "budget": child_budget.model_dump(mode="json"),
        },
        child_script=FakeScript(steps=(FakeFailureStep(failure=FakeFailureKind.AUTHENTICATION),)),
    )
    try:
        parent = await service.run(
            RunRequest(
                prompt="delegate failing work",
                workspace=tmp_path,
                lease=CapabilityLease(can_delegate=True, max_child_depth=1),
                budget=_budget(requests=4, tools=1, children=1, depth=1),
            )
        )
    finally:
        await providers.aclose()

    assert parent.run.status.value == "completed"
    async with database.session() as session:
        delegation = (await AgentDelegationRepository(session).list_for_parent(parent.run.id))[0]
    assert delegation.status is DelegationStatus.FAILED
    assert delegation.result is not None
    assert delegation.result.error_code == "provider_authentication_error"


def _approval_service(
    database: Database,
    *,
    parent_script: FakeScript,
    child_script: FakeScript,
    action: ChildAction,
) -> tuple[RunService, ProviderFactory]:
    parent_model = FakeModelSpec(model="parent-approval-delegation")
    child_model = FakeModelSpec(model="child-approval-delegation")
    providers = ProviderFactory(
        {"parent": parent_model, "child": child_model},
        adapters=(
            FakeProviderAdapter(
                {
                    parent_model.model: parent_script,
                    child_model.model: child_script,
                }
            ),
        ),
    )
    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            name="test.child_action",
            description="Perform one approved child action.",
            params_model=ChildActionParams,
            policy=ToolPolicy(
                risk=RiskLevel.GUARDED,
                requires_approval=True,
                idempotent=True,
                timeout_seconds=5,
                max_output_bytes=1_024,
                capabilities=frozenset({Capability.FILESYSTEM_READ}),
            ),
            handler=action,
        )
    )
    parent = AgentSpec(
        id="coordinator",
        name="Coordinator",
        description="Delegates an approval test task.",
        model_ref="parent",
        instructions="Delegate the task.",
        tools=("agent.delegate",),
        capabilities=frozenset({Capability.AGENT_DELEGATE}),
        max_children=1,
        max_depth=1,
    )
    child = AgentSpec(
        id="worker",
        name="Worker",
        description="Requests one approved action.",
        model_ref="child",
        instructions="Call the child action.",
        tools=("test.child_action",),
        capabilities=frozenset({Capability.FILESYSTEM_READ}),
    )
    return (
        RunService(
            database=database,
            providers=providers,
            agents=AgentRegistry((parent, child)),
            tools=tools,
        ),
        providers,
    )


@pytest.mark.asyncio
async def test_child_approval_resumes_child_then_parent_from_delegation_checkpoint(
    database: Database,
    tmp_path: Path,
) -> None:
    child_budget = _budget(requests=2, tools=1, children=0, depth=0).model_copy(
        update={
            "max_input_tokens": 400,
            "max_output_tokens": 400,
            "max_total_tokens": 800,
            "max_duration_seconds": 30,
        }
    )
    child_lease = CapabilityLease(filesystem_read=(".",))
    delegate_arguments = cast(
        dict[str, JsonValue],
        {
            "agent_id": "worker",
            "task": "perform approved child work",
            "lease": child_lease.model_dump(mode="json"),
            "budget": child_budget.model_dump(mode="json"),
        },
    )
    action = ChildAction()
    first, first_providers = _approval_service(
        database,
        parent_script=FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="agent.delegate",
                    arguments=delegate_arguments,
                    tool_call_id="delegate-approval-1",
                ),
            )
        ),
        child_script=FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="test.child_action",
                    tool_call_id="child-action-1",
                ),
            )
        ),
        action=action,
    )
    try:
        paused = await first.run(
            RunRequest(
                prompt="delegate approval work",
                workspace=tmp_path,
                lease=CapabilityLease(
                    filesystem_read=(".",),
                    can_delegate=True,
                    max_child_depth=1,
                ),
                budget=_budget(requests=4, tools=2, children=1, depth=1),
            )
        )
    finally:
        await first_providers.aclose()

    assert paused.run.status.value == "paused"
    async with database.session() as session:
        delegation = (
            await AgentDelegationRepository(session).list_active_for_parent(paused.run.id)
        )[0]
    assert delegation.status is DelegationStatus.WAITING_APPROVAL
    assert delegation.result is not None
    approval_id = delegation.result.approval_ids[0]

    restarted, restarted_providers = _approval_service(
        database,
        parent_script=FakeScript(steps=(FakeTextStep(text="parent resumed"),)),
        child_script=FakeScript(steps=(FakeTextStep(text="child resumed"),)),
        action=action,
    )
    try:
        completed = await restarted.resume(
            approval_id,
            ApprovalDecision(kind=ApprovalDecisionKind.APPROVE),
        )
    finally:
        await restarted_providers.aclose()

    assert completed.run.id == paused.run.id
    assert completed.run.status.value == "completed"
    assert completed.output == "parent resumed"
    assert action.calls == 1
