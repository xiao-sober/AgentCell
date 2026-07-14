"""Deterministic event-prefix replay and checkpoint-backed branch tests."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from agentcell.agents import AgentRegistry, coordinator_spec
from agentcell.budgets import Budget, BudgetTracker
from agentcell.events import (
    EventType,
    GenericEventPayload,
    RunCompletedPayload,
    RunStartedPayload,
    RunStatusChangedPayload,
)
from agentcell.kernel.checkpoint import Checkpoint, CheckpointKind
from agentcell.kernel.lifecycle import RunStatus
from agentcell.kernel.models import Run
from agentcell.kernel.replay import ReplayService
from agentcell.kernel.run_service import RunService
from agentcell.policy import CapabilityLease
from agentcell.providers import (
    FakeModelSpec,
    FakeProviderAdapter,
    FakeScript,
    FakeTextStep,
    ProviderFactory,
)
from agentcell.storage import CheckpointRepository, Database, EventStore, RunRepository
from agentcell.tools import ToolRegistry, register_workspace_tools


def _budget() -> Budget:
    return Budget(
        max_requests=10,
        max_input_tokens=1_000,
        max_output_tokens=1_000,
        max_total_tokens=2_000,
        max_tool_calls=10,
        max_duration_seconds=60,
        max_cost=None,
        max_children=0,
        max_depth=0,
    )


@pytest.mark.asyncio
async def test_replay_projects_same_terminal_state_and_prefix(database: Database) -> None:
    run = Run(conversation_id=uuid4(), agent_id="coordinator")
    running = run.transition_to(RunStatus.RUNNING)
    completed = running.transition_to(RunStatus.COMPLETED)
    async with database.transaction() as session:
        runs = RunRepository(session)
        events = EventStore(session)
        await runs.create(run)
        await events.append(
            run_id=run.id,
            event_type=EventType.RUN_STARTED,
            payload=RunStartedPayload(
                conversation_id=run.conversation_id,
                agent_id=run.agent_id,
            ),
        )
        await runs.save(running)
        await events.append(
            run_id=run.id,
            event_type=EventType.RUN_STATUS_CHANGED,
            payload=RunStatusChangedPayload(
                previous_status=RunStatus.CREATED,
                status=RunStatus.RUNNING,
            ),
        )
        await runs.save(completed)
        await events.append(
            run_id=run.id,
            event_type=EventType.RUN_STATUS_CHANGED,
            payload=RunStatusChangedPayload(
                previous_status=RunStatus.RUNNING,
                status=RunStatus.COMPLETED,
            ),
        )
        await events.append(
            run_id=run.id,
            event_type=EventType.RUN_COMPLETED,
            payload=RunCompletedPayload(
                output_characters=4,
                requests=1,
                input_tokens=1,
                output_tokens=1,
                tool_calls=0,
            ),
        )

    replay = ReplayService(database)
    terminal = await replay.replay(run.id)
    prefix = await replay.replay(run.id, through_sequence=2)

    assert terminal.status is completed.status
    assert terminal.events_applied == 4
    assert prefix.status is RunStatus.RUNNING
    assert prefix.events_applied == 2


@pytest.mark.asyncio
async def test_branch_references_only_requested_source_prefix(
    database: Database,
    tmp_path: Path,
) -> None:
    source = Run(conversation_id=uuid4(), agent_id="coordinator")
    running = source.transition_to(RunStatus.RUNNING)
    waiting = running.transition_to(RunStatus.WAITING_APPROVAL)
    async with database.transaction() as session:
        runs = RunRepository(session)
        events = EventStore(session)
        await runs.create(source)
        await events.append(
            run_id=source.id,
            event_type=EventType.RUN_STARTED,
            payload=RunStartedPayload(
                conversation_id=source.conversation_id,
                agent_id=source.agent_id,
            ),
        )
        await runs.save(running)
        await events.append(
            run_id=source.id,
            event_type=EventType.RUN_STATUS_CHANGED,
            payload=RunStatusChangedPayload(
                previous_status=RunStatus.CREATED,
                status=RunStatus.RUNNING,
            ),
        )
        checkpoint_event = await events.append(
            run_id=source.id,
            event_type=EventType.CHECKPOINT_CREATED,
            payload=GenericEventPayload(data={"reason": "test"}),
        )
        await CheckpointRepository(session).create(
            Checkpoint(
                run_id=source.id,
                user_id=uuid4(),
                event_sequence=checkpoint_event.sequence,
                kind=CheckpointKind.APPROVAL,
                agent_id=source.agent_id,
                prompt="continue",
                workspace=str(tmp_path),
                lease=CapabilityLease(filesystem_read=(".",)),
                budget=BudgetTracker(_budget()).snapshot(),
                messages=[],
                run_status=RunStatus.WAITING_APPROVAL,
            )
        )
        await runs.save(waiting)
        await events.append(
            run_id=source.id,
            event_type=EventType.RUN_STATUS_CHANGED,
            payload=RunStatusChangedPayload(
                previous_status=RunStatus.RUNNING,
                status=RunStatus.WAITING_APPROVAL,
            ),
        )

    replay = ReplayService(database)
    child = await replay.branch(source.id, from_sequence=3)

    assert child.parent_run_id == source.id
    assert child.status is RunStatus.PAUSED
    async with database.session() as session:
        checkpoint = await CheckpointRepository(session).latest(child.id)
    assert checkpoint.source_run_id == source.id
    assert checkpoint.source_sequence == 3
    assert checkpoint.run_status is RunStatus.PAUSED

    model = FakeModelSpec(model="branch-resume")
    providers = ProviderFactory(
        {"fake": model},
        adapters=(
            FakeProviderAdapter(
                {model.model: FakeScript(steps=(FakeTextStep(text="branched result"),))}
            ),
        ),
    )
    tools = ToolRegistry()
    register_workspace_tools(tools)
    service = RunService(
        database=database,
        providers=providers,
        agents=AgentRegistry((coordinator_spec(model_ref="fake"),)),
        tools=tools,
    )
    try:
        resumed = await service.resume_paused(child.id)
    finally:
        await providers.aclose()

    assert resumed.run.status is RunStatus.COMPLETED
    assert resumed.output == "branched result"
