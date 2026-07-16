"""Stage 8 deterministic multi-Agent handoff workflow tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field

from agentcell.agents import (
    AgentDelegation,
    AgentRegistry,
    AgentSpec,
    DelegationResult,
    DelegationStatus,
    HandoffRequest,
    HandoffStage,
)
from agentcell.budgets import Budget
from agentcell.events import EventType, JsonValue
from agentcell.kernel.handoff import HandoffService
from agentcell.kernel.lifecycle import RunStatus
from agentcell.kernel.replay import ReplayService
from agentcell.kernel.run_service import RunService
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
    ApprovalRepository,
    Database,
    EventStore,
    RunRepository,
)
from agentcell.tools import (
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
    register_workspace_tools,
)


class ActionParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: int = Field(ge=1, strict=True)


class EmptyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass
class ActionCounter:
    values: list[int]

    async def handle(self, params: ActionParams, context: ToolExecutionContext) -> JsonValue:
        del context
        self.values.append(params.value)
        return {"accepted": params.value}


async def successful_test(
    params: EmptyParams,
    context: ToolExecutionContext,
) -> JsonValue:
    del params, context
    return {
        "command": ["pytest", "tests/"],
        "cwd": ".",
        "exit_code": 0,
        "stdout": "44 passed, 4 skipped",
        "stderr": "",
        "output_bytes": 20,
        "test_execution": {
            "framework": "pytest",
            "executed": True,
            "successful": True,
            "collected_only": False,
            "summary": "44 passed, 4 skipped",
        },
    }


def _budget(*, requests: int, children: int, depth: int) -> Budget:
    return Budget(
        max_requests=requests,
        max_input_tokens=2_000,
        max_output_tokens=2_000,
        max_total_tokens=4_000,
        max_tool_calls=0,
        max_duration_seconds=120,
        max_cost=None,
        max_children=children,
        max_depth=depth,
    )


class SimulatedProcessExit(BaseException):
    """Fault injection that bypasses in-process exception cleanup."""


def _handoff_service(
    database: Database,
    *,
    reviewer_output: str = "PASS\nreviewer complete",
    coder_script: FakeScript | None = None,
    counter: ActionCounter | None = None,
) -> tuple[HandoffService, RunService, ProviderFactory]:
    specs: list[AgentSpec] = []
    models: dict[str, FakeModelSpec] = {}
    scripts: dict[str, FakeScript] = {}
    for stage in HandoffStage:
        model_ref = f"model-{stage.value}"
        model = FakeModelSpec(model=model_ref)
        models[model_ref] = model
        output = f"{stage.value} complete"
        if stage is HandoffStage.REVIEWER:
            output = reviewer_output
        scripts[model.model] = (
            coder_script
            if stage is HandoffStage.CODER and coder_script is not None
            else FakeScript(steps=(FakeTextStep(text=output),))
        )
        tools: tuple[str, ...] = ()
        capabilities: frozenset[Capability] = frozenset()
        if stage is HandoffStage.CODER and counter is not None:
            tools = ("test.action",)
            capabilities = frozenset({Capability.FILESYSTEM_READ})
        specs.append(
            AgentSpec(
                id=stage.value,
                name=stage.value.title(),
                description=f"Test {stage.value} stage.",
                model_ref=model_ref,
                instructions=f"Complete the {stage.value} stage.",
                tools=tools,
                capabilities=capabilities,
            )
        )
    providers = ProviderFactory(models, adapters=(FakeProviderAdapter(scripts),))
    registry = ToolRegistry()
    if counter is not None:
        registry.register(
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
    runs = RunService(
        database=database,
        providers=providers,
        agents=AgentRegistry(specs),
        tools=registry,
    )
    return HandoffService(database, runs), runs, providers


def _green_test_handoff_service(
    database: Database,
) -> tuple[HandoffService, ProviderFactory]:
    models: dict[str, FakeModelSpec] = {}
    scripts: dict[str, FakeScript] = {}
    specs: list[AgentSpec] = []
    for stage in HandoffStage:
        model_ref = f"green-{stage.value}"
        model = FakeModelSpec(model=model_ref)
        models[model_ref] = model
        tools: tuple[str, ...] = ()
        capabilities: frozenset[Capability] = frozenset()
        steps: tuple[FakeTextStep | FakeToolCallStep, ...]
        if stage is HandoffStage.CODER:
            tools = ("shell.test",)
            capabilities = frozenset({Capability.SHELL_EXECUTE})
            steps = (
                FakeToolCallStep(tool_name="shell.test", arguments={}),
                FakeToolCallStep(
                    tool_name="shell.test",
                    arguments={},
                    tool_call_id="coder-tool-must-be-hidden",
                ),
                FakeTextStep(text="No repair was required."),
            )
        elif stage is HandoffStage.REVIEWER:
            tools = ("workspace.list",)
            capabilities = frozenset({Capability.FILESYSTEM_READ})
            steps = (
                FakeToolCallStep(tool_name="workspace.list", arguments={"path": "."}),
                FakeTextStep(text="PASS\nPersisted test and change evidence is sufficient."),
            )
        else:
            steps = (FakeTextStep(text=f"{stage.value} complete"),)
        scripts[model.model] = FakeScript(steps=steps)
        specs.append(
            AgentSpec(
                id=stage.value,
                name=stage.value.title(),
                description=f"Test {stage.value} stage.",
                model_ref=model_ref,
                instructions=f"Complete the {stage.value} stage.",
                tools=tools,
                capabilities=capabilities,
                max_steps=5,
            )
        )
    providers = ProviderFactory(models, adapters=(FakeProviderAdapter(scripts),))
    registry = ToolRegistry()
    register_workspace_tools(registry)
    registry.register(
        ToolDefinition(
            name="shell.test",
            description="Return one successful persisted test result.",
            params_model=EmptyParams,
            policy=ToolPolicy(
                risk=RiskLevel.SAFE,
                requires_approval=False,
                idempotent=True,
                timeout_seconds=5,
                max_output_bytes=1_024,
                capabilities=frozenset({Capability.SHELL_EXECUTE}),
            ),
            handler=successful_test,
        )
    )
    runs = RunService(
        database=database,
        providers=providers,
        agents=AgentRegistry(specs),
        tools=registry,
    )
    return HandoffService(database, runs), providers


def _handoff_request(tmp_path: Path) -> HandoffRequest:
    stage_budget = _budget(requests=1, children=0, depth=0).model_copy(
        update={
            "max_input_tokens": 200,
            "max_output_tokens": 200,
            "max_total_tokens": 400,
            "max_duration_seconds": 30,
        }
    )
    return HandoffRequest(
        task="implement and review",
        workspace=str(tmp_path),
        lease=CapabilityLease(),
        budget=_budget(requests=4, children=4, depth=1),
        stage_budgets={stage: stage_budget for stage in HandoffStage},
        stage_leases={stage: CapabilityLease() for stage in HandoffStage},
        stage_instructions={stage: f"Stage directive for {stage.value}." for stage in HandoffStage},
        stage_output_contracts={stage: f"Contract for {stage.value}." for stage in HandoffStage},
    )


def _approval_handoff_request(tmp_path: Path) -> HandoffRequest:
    request = _handoff_request(tmp_path)
    stage_budgets = dict(request.stage_budgets)
    stage_budgets[HandoffStage.CODER] = stage_budgets[HandoffStage.CODER].model_copy(
        update={"max_requests": 2, "max_tool_calls": 1}
    )
    stage_leases = dict(request.stage_leases)
    stage_leases[HandoffStage.CODER] = CapabilityLease(filesystem_read=(".",))
    return request.model_copy(
        update={
            "lease": CapabilityLease(filesystem_read=(".",)),
            "budget": request.budget.model_copy(update={"max_requests": 5, "max_tool_calls": 1}),
            "stage_budgets": stage_budgets,
            "stage_leases": stage_leases,
        }
    )


@pytest.mark.asyncio
async def test_programmatic_handoff_runs_four_linked_stages_in_order(
    database: Database,
    tmp_path: Path,
) -> None:
    handoff, _, providers = _handoff_service(database)
    try:
        result = await handoff.run(_handoff_request(tmp_path))
    finally:
        await providers.aclose()

    assert result.status is DelegationStatus.COMPLETED
    assert [stage.agent_id for stage in result.stages] == [
        "coordinator",
        "coder",
        "reviewer",
        "finalizer",
    ]
    assert result.output == "finalizer complete"
    async with database.session() as session:
        delegations = await AgentDelegationRepository(session).list_for_parent(result.root_run_id)
        events = await EventStore(session).list_for_run(result.root_run_id)
        children = [await RunRepository(session).get(item.child_run_id) for item in delegations]
    assert [item.target_agent_id for item in delegations] == [
        "coordinator",
        "coder",
        "reviewer",
        "finalizer",
    ]
    assert all(item.parent_run_id == result.root_run_id for item in delegations)
    assert all(child is not None and child.execution_identity is not None for child in children)
    assert [
        child.execution_identity.agent_spec.model_ref
        for child in children
        if child is not None and child.execution_identity is not None
    ] == [f"model-{stage.value}" for stage in HandoffStage]
    assert "[coordinator]\ncoordinator complete" in delegations[-1].task
    assert "[coder]\ncoder complete" in delegations[-1].task
    assert "[reviewer]\nPASS\nreviewer complete" in delegations[-1].task
    assert "Stage instructions: Stage directive for finalizer." in delegations[-1].task
    assert "Required output: Contract for finalizer." in delegations[-1].task
    assert [event.event_type for event in events].count(EventType.AGENT_CHILD_STARTED) == 4
    assert [event.event_type for event in events].count(EventType.AGENT_CHILD_COMPLETED) == 4
    assert events[-1].event_type is EventType.RUN_COMPLETED
    replayed = await ReplayService(database).replay(result.root_run_id)
    assert replayed.status is RunStatus.COMPLETED
    assert replayed.events_applied == len(events)


@pytest.mark.asyncio
async def test_green_test_stops_coder_and_gives_reviewer_no_tool_evidence_path(
    database: Database,
    tmp_path: Path,
) -> None:
    handoff, providers = _green_test_handoff_service(database)
    request = _handoff_request(tmp_path)
    stage_resources = {
        "max_input_tokens": 5_000,
        "max_output_tokens": 1_000,
        "max_total_tokens": 6_000,
        "max_duration_seconds": 30,
    }
    stage_budgets = {
        HandoffStage.COORDINATOR: _budget(requests=1, children=0, depth=0).model_copy(
            update=stage_resources
        ),
        HandoffStage.CODER: _budget(requests=3, children=0, depth=0).model_copy(
            update={**stage_resources, "max_tool_calls": 2}
        ),
        HandoffStage.REVIEWER: _budget(requests=2, children=0, depth=0).model_copy(
            update={**stage_resources, "max_tool_calls": 1}
        ),
        HandoffStage.FINALIZER: _budget(requests=1, children=0, depth=0).model_copy(
            update=stage_resources
        ),
    }
    stage_leases = dict(request.stage_leases)
    stage_leases[HandoffStage.CODER] = CapabilityLease(commands=frozenset({"pytest"}))
    stage_leases[HandoffStage.REVIEWER] = CapabilityLease(filesystem_read=(".",))
    request = request.model_copy(
        update={
            "task": "fix the failing tests and independently review",
            "lease": CapabilityLease(filesystem_read=(".",), commands=frozenset({"pytest"})),
            "budget": _budget(requests=7, children=4, depth=1).model_copy(
                update={
                    "max_tool_calls": 3,
                    "max_input_tokens": 20_000,
                    "max_output_tokens": 4_000,
                    "max_total_tokens": 24_000,
                }
            ),
            "stage_budgets": stage_budgets,
            "stage_leases": stage_leases,
        }
    )
    try:
        result = await handoff.run(request)
    finally:
        await providers.aclose()

    assert result.status is DelegationStatus.COMPLETED
    assert result.stages[1].usage.tool_calls == 1
    assert result.stages[2].usage.tool_calls == 0
    async with database.session() as session:
        delegations = await AgentDelegationRepository(session).list_for_parent(result.root_run_id)
        root_events = await EventStore(session).list_for_run(result.root_run_id)
    reviewer = next(item for item in delegations if item.target_agent_id == "reviewer")
    assert "persisted shell.test" in reviewer.task
    assert "exit_code=0" in reviewer.task
    assert "file_changes=0" in reviewer.task
    projected_tools = [
        event
        for event in root_events
        if event.event_type in {EventType.TOOL_PROPOSED, EventType.TOOL_COMPLETED}
    ]
    assert [event.event_type for event in projected_tools] == [
        EventType.TOOL_PROPOSED,
        EventType.TOOL_COMPLETED,
    ]
    for event in projected_tools:
        data = event.safe_payload().get("data")
        assert isinstance(data, dict)
        assert data["tool_name"] == "shell.test"
        assert data["source_agent_id"] == "coder"
        assert "output" not in data


@pytest.mark.asyncio
async def test_reviewer_changes_requested_stops_before_finalizer(
    database: Database,
    tmp_path: Path,
) -> None:
    handoff, _, providers = _handoff_service(
        database,
        reviewer_output="CHANGES_NEEDED\nMissing regression coverage.",
    )
    try:
        result = await handoff.run(_handoff_request(tmp_path))
    finally:
        await providers.aclose()

    assert result.status is DelegationStatus.FAILED
    assert result.error_code == "reviewer_changes_requested"
    assert result.error_message == "Reviewer requested changes; finalization was not started"
    assert result.error_stage is HandoffStage.REVIEWER
    assert [item.agent_id for item in result.stages] == [
        "coordinator",
        "coder",
        "reviewer",
    ]
    assert result.output == "CHANGES_NEEDED\nMissing regression coverage."
    async with database.session() as session:
        root = await RunRepository(session).get(result.root_run_id)
        delegations = await AgentDelegationRepository(session).list_for_parent(result.root_run_id)
        events = await EventStore(session).list_for_run(result.root_run_id)
    assert root is not None and root.status is RunStatus.FAILED
    assert [item.target_agent_id for item in delegations] == [
        "coordinator",
        "coder",
        "reviewer",
    ]
    assert events[-1].event_type is EventType.RUN_FAILED
    assert events[-1].payload.model_dump()["code"] == "reviewer_changes_requested"


@pytest.mark.asyncio
async def test_child_budget_exhaustion_fails_root_without_starting_later_stages(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = ActionCounter([])
    handoff, _, providers = _handoff_service(
        database,
        coder_script=FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="test.action",
                    arguments={"value": 3},
                    tool_call_id="handoff-budget-1",
                ),
            )
        ),
        counter=counter,
    )
    request = _handoff_request(tmp_path)
    stage_budgets = dict(request.stage_budgets)
    stage_budgets[HandoffStage.CODER] = stage_budgets[HandoffStage.CODER].model_copy(
        update={"max_tool_calls": 1}
    )
    stage_leases = dict(request.stage_leases)
    stage_leases[HandoffStage.CODER] = CapabilityLease(filesystem_read=(".",))
    request = request.model_copy(
        update={
            "lease": CapabilityLease(filesystem_read=(".",)),
            "budget": request.budget.model_copy(update={"max_tool_calls": 1}),
            "stage_budgets": stage_budgets,
            "stage_leases": stage_leases,
        }
    )
    try:
        result = await handoff.run(request)
    finally:
        await providers.aclose()

    assert result.status is DelegationStatus.FAILED
    assert [item.agent_id for item in result.stages] == ["coordinator", "coder"]
    assert result.stages[-1].error_code == "budget_exceeded"
    assert result.error_code == "budget_exceeded"
    assert result.error_stage is HandoffStage.CODER
    assert counter.values == []


@pytest.mark.asyncio
async def test_handoff_resumes_when_child_completed_before_stage_settlement(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff, runs, providers = _handoff_service(database)
    recover = runs.recover_delegation_child
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

    monkeypatch.setattr(runs, "recover_delegation_child", stop_after_child_completion)
    try:
        with pytest.raises(SimulatedProcessExit):
            await handoff.run(_handoff_request(tmp_path))
    finally:
        await providers.aclose()

    delegation = captured["delegation"]
    async with database.session() as session:
        durable = await AgentDelegationRepository(session).get_required(delegation.id)
        child = await RunRepository(session).get(delegation.child_run_id)
    assert child is not None
    assert child.status is RunStatus.COMPLETED
    assert durable.result is not None
    assert durable.result.output == "coordinator complete"

    restarted, _, restarted_providers = _handoff_service(database)
    try:
        result = await restarted.resume(delegation.parent_run_id)
    finally:
        await restarted_providers.aclose()

    assert result.status is DelegationStatus.COMPLETED
    assert [stage.agent_id for stage in result.stages] == [
        "coordinator",
        "coder",
        "reviewer",
        "finalizer",
    ]
    assert result.output == "finalizer complete"


@pytest.mark.asyncio
async def test_handoff_propagates_child_approval_across_process_restart(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = ActionCounter([])
    first, _, providers = _handoff_service(
        database,
        coder_script=FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="test.action",
                    arguments={"value": 7},
                    tool_call_id="handoff-approval-1",
                ),
            )
        ),
        counter=counter,
    )
    try:
        waiting = await first.run(_approval_handoff_request(tmp_path))
    finally:
        await providers.aclose()

    assert waiting.status is DelegationStatus.WAITING_APPROVAL, waiting.model_dump_json(indent=2)
    assert [item.agent_id for item in waiting.stages] == ["coordinator", "coder"]
    assert len(waiting.stages[-1].approval_ids) == 1
    approval_id = waiting.stages[-1].approval_ids[0]
    async with database.session() as session:
        root = await RunRepository(session).get(waiting.root_run_id)
        approval = await ApprovalRepository(session).get_required(approval_id)
    assert root is not None and root.status is RunStatus.PAUSED
    assert approval.run_id == waiting.stages[-1].child_run_id

    restarted, restarted_runs, restarted_providers = _handoff_service(
        database,
        coder_script=FakeScript(steps=(FakeTextStep(text="coder complete"),)),
        counter=counter,
    )
    try:
        await restarted_runs.resume(
            approval_id,
            ApprovalDecision(kind=ApprovalDecisionKind.APPROVE),
        )
        completed = await restarted.resume(waiting.root_run_id)
    finally:
        await restarted_providers.aclose()

    assert completed.status is DelegationStatus.COMPLETED
    assert counter.values == [7]
    assert completed.output == "finalizer complete"
    async with database.session() as session:
        delegations = await AgentDelegationRepository(session).list_for_parent(
            completed.root_run_id
        )
    coder = next(item for item in delegations if item.target_agent_id == "coder")
    assert coder.result is not None
    assert coder.accounted_usage == coder.result.usage


@pytest.mark.asyncio
async def test_handoff_child_failure_after_approval_converges_root(
    database: Database,
    tmp_path: Path,
) -> None:
    counter = ActionCounter([])
    first, _, providers = _handoff_service(
        database,
        coder_script=FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="test.action",
                    arguments={"value": 7},
                    tool_call_id="handoff-approval-failure-1",
                ),
            )
        ),
        counter=counter,
    )
    try:
        waiting = await first.run(_approval_handoff_request(tmp_path))
    finally:
        await providers.aclose()

    approval_id = waiting.stages[-1].approval_ids[0]
    restarted, _, restarted_providers = _handoff_service(
        database,
        coder_script=FakeScript(steps=(FakeFailureStep(failure=FakeFailureKind.AUTHENTICATION),)),
        counter=counter,
    )
    try:
        failed = await restarted.decide_approval(
            waiting.root_run_id,
            approval_id,
            ApprovalDecision(kind=ApprovalDecisionKind.APPROVE),
        )
    finally:
        await restarted_providers.aclose()

    assert failed.status is DelegationStatus.FAILED
    assert failed.error_code == "provider_authentication_error"
    assert failed.error_stage is HandoffStage.CODER
    assert counter.values == [7]
    async with database.session() as session:
        root = await RunRepository(session).get(waiting.root_run_id)
        child = await RunRepository(session).get(waiting.stages[-1].child_run_id)
        events = await EventStore(session).list_for_run(waiting.root_run_id)
        delegations = await AgentDelegationRepository(session).list_for_parent(waiting.root_run_id)
    assert root is not None and root.status is RunStatus.FAILED
    assert child is not None and child.status is RunStatus.FAILED
    assert events[-1].event_type is EventType.RUN_FAILED
    coder = next(item for item in delegations if item.target_agent_id == "coder")
    assert coder.result is not None
    assert coder.accounted_usage == coder.result.usage


@pytest.mark.asyncio
async def test_cancelling_handoff_converges_root_child_and_delegation(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff, runs, providers = _handoff_service(database)
    started = asyncio.Event()
    blocked = asyncio.Event()
    captured: dict[str, AgentDelegation] = {}

    async def block_child(
        delegation: AgentDelegation,
        *,
        workspace: Path,
        user_id: UUID,
        conversation_id: UUID,
    ) -> DelegationResult:
        del workspace, user_id, conversation_id
        captured["delegation"] = delegation
        started.set()
        await blocked.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(runs, "recover_delegation_child", block_child)
    task = asyncio.create_task(handoff.run(_handoff_request(tmp_path)))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await providers.aclose()

    delegation = captured["delegation"]
    async with database.session() as session:
        root = await RunRepository(session).get(delegation.parent_run_id)
        child = await RunRepository(session).get(delegation.child_run_id)
        durable = await AgentDelegationRepository(session).get_required(delegation.id)
        events = await EventStore(session).list_for_run(delegation.parent_run_id)
    assert root is not None
    assert child is not None
    assert root.status is RunStatus.CANCELLED
    assert child.status is RunStatus.CANCELLED
    assert durable.status is DelegationStatus.CANCELLED
    assert events[-1].event_type is EventType.RUN_CANCELLED
