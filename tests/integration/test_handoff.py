"""Stage 8 deterministic multi-Agent handoff workflow tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import pytest

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
from agentcell.events import EventType
from agentcell.kernel.handoff import HandoffService
from agentcell.kernel.lifecycle import RunStatus
from agentcell.kernel.run_service import RunService
from agentcell.policy import CapabilityLease
from agentcell.providers import (
    FakeModelSpec,
    FakeProviderAdapter,
    FakeScript,
    FakeTextStep,
    ProviderFactory,
)
from agentcell.storage import AgentDelegationRepository, Database, EventStore, RunRepository
from agentcell.tools import ToolRegistry


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
) -> tuple[HandoffService, RunService, ProviderFactory]:
    specs: list[AgentSpec] = []
    models: dict[str, FakeModelSpec] = {}
    scripts: dict[str, FakeScript] = {}
    for stage in HandoffStage:
        model_ref = f"model-{stage.value}"
        model = FakeModelSpec(model=model_ref)
        models[model_ref] = model
        scripts[model.model] = FakeScript(steps=(FakeTextStep(text=f"{stage.value} complete"),))
        specs.append(
            AgentSpec(
                id=stage.value,
                name=stage.value.title(),
                description=f"Test {stage.value} stage.",
                model_ref=model_ref,
                instructions=f"Complete the {stage.value} stage.",
            )
        )
    providers = ProviderFactory(models, adapters=(FakeProviderAdapter(scripts),))
    runs = RunService(
        database=database,
        providers=providers,
        agents=AgentRegistry(specs),
        tools=ToolRegistry(),
    )
    return HandoffService(database, runs), runs, providers


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
    assert [item.target_agent_id for item in delegations] == [
        "coordinator",
        "coder",
        "reviewer",
        "finalizer",
    ]
    assert all(item.parent_run_id == result.root_run_id for item in delegations)
    assert [event.event_type for event in events].count(EventType.AGENT_CHILD_STARTED) == 4
    assert [event.event_type for event in events].count(EventType.AGENT_CHILD_COMPLETED) == 4
    assert events[-1].event_type is EventType.RUN_COMPLETED


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
