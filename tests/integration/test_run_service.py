"""Stage 5 deterministic RunService lifecycle and read-only tool-loop tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from agentcell.agents import AgentRegistry, AgentSpec
from agentcell.budgets import Budget
from agentcell.errors import (
    BudgetExceededError,
    ModelOutputError,
    ProviderAuthenticationError,
    WorkspacePathNotFoundError,
)
from agentcell.events import ErrorPayload, EventType
from agentcell.kernel.run_service import RunRequest, RunService
from agentcell.policy import Capability, CapabilityLease, RiskLevel, ToolPolicy
from agentcell.providers import (
    FakeFailureKind,
    FakeFailureStep,
    FakeModelSpec,
    FakeProviderAdapter,
    FakeScript,
    FakeTextStep,
    FakeToolCallStep,
    ModelUsage,
    ProviderFactory,
)
from agentcell.storage import Database, EventStore
from agentcell.storage.tables import RunRow
from agentcell.tools import (
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
    register_workspace_tools,
)


def _runtime(
    database: Database,
    script: FakeScript,
    *,
    tool_names: tuple[str, ...] = (),
    max_steps: int = 10,
) -> tuple[RunService, ProviderFactory]:
    model = FakeModelSpec(model="run-script")
    providers = ProviderFactory(
        {"fake_runtime": model},
        adapters=[FakeProviderAdapter({model.model: script})],
    )
    agent = AgentSpec(
        id="coordinator",
        name="Coordinator",
        description="Test coordinator.",
        model_ref="fake_runtime",
        instructions="Complete the task using only registered tools.",
        tools=tool_names,
        capabilities=frozenset({Capability.FILESYSTEM_READ}) if tool_names else frozenset(),
        max_steps=max_steps,
    )
    registry = ToolRegistry()
    register_workspace_tools(registry)
    service = RunService(
        database=database,
        providers=providers,
        agents=AgentRegistry([agent]),
        tools=registry,
    )
    return service, providers


@pytest.mark.asyncio
async def test_text_run_persists_complete_lifecycle(database: Database, tmp_path: Path) -> None:
    script = FakeScript(
        steps=(
            FakeTextStep(
                text="done",
                chunks=("do", "ne"),
                usage=ModelUsage(input_tokens=4, output_tokens=2),
            ),
        )
    )
    service, providers = _runtime(database, script)
    try:
        result = await service.run(
            RunRequest(
                prompt="finish",
                workspace=tmp_path,
                lease=CapabilityLease(),
            )
        )
    finally:
        await providers.aclose()

    assert result.output == "done"
    assert result.run.status.value == "completed"
    assert result.budget.used.requests == 1
    assert result.budget.used.input_tokens > 0
    assert result.budget.used.output_tokens > 0

    async with database.session() as session:
        events = await EventStore(session).list_for_run(result.run.id)
    event_types = [event.event_type for event in events]
    assert event_types == [
        EventType.RUN_STARTED,
        EventType.RUN_STATUS_CHANGED,
        EventType.BUDGET_UPDATED,
        EventType.MODEL_REQUESTED,
        EventType.MODEL_TEXT_DELTA,
        EventType.MODEL_TEXT_DELTA,
        EventType.MODEL_COMPLETED,
        EventType.BUDGET_UPDATED,
        EventType.RUN_STATUS_CHANGED,
        EventType.RUN_COMPLETED,
    ]
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))


@pytest.mark.asyncio
async def test_read_only_tool_loop_uses_tool_executor(database: Database, tmp_path: Path) -> None:
    (tmp_path / "README.txt").write_text("AgentCell runtime", encoding="utf-8")
    script = FakeScript(
        steps=(
            FakeToolCallStep(
                tool_name="workspace.read",
                arguments={"path": "README.txt"},
                usage=ModelUsage(input_tokens=2, output_tokens=1),
            ),
            FakeTextStep(
                text="read complete",
                usage=ModelUsage(input_tokens=3, output_tokens=2),
            ),
        )
    )
    service, providers = _runtime(database, script, tool_names=("workspace.read",))
    try:
        result = await service.run(
            RunRequest(
                prompt="read the file",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
            )
        )
    finally:
        await providers.aclose()

    assert result.output == "read complete"
    assert result.budget.used.requests == 2
    assert result.budget.used.tool_calls == 1
    async with database.session() as session:
        events = await EventStore(session).list_for_run(result.run.id)
    event_types = [event.event_type for event in events]
    assert event_types.count(EventType.MODEL_REQUESTED) == 2
    assert event_types.count(EventType.MODEL_COMPLETED) == 2
    tool_slice = [
        EventType.TOOL_PROPOSED,
        EventType.BUDGET_UPDATED,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
    ]
    start = event_types.index(EventType.TOOL_PROPOSED)
    assert event_types[start : start + 4] == tool_slice


@pytest.mark.asyncio
async def test_multiple_read_only_tool_calls_share_one_run_budget(
    database: Database,
    tmp_path: Path,
) -> None:
    (tmp_path / "README.txt").write_text("AgentCell runtime", encoding="utf-8")
    service, providers = _runtime(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(tool_name="workspace.list", arguments={"path": "."}),
                FakeToolCallStep(
                    tool_name="workspace.read",
                    arguments={"path": "README.txt"},
                    tool_call_id="fake-tool-call-2",
                ),
                FakeTextStep(text="two tools complete"),
            )
        ),
        tool_names=("workspace.list", "workspace.read"),
    )
    try:
        result = await service.run(
            RunRequest(
                prompt="list and read",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
            )
        )
    finally:
        await providers.aclose()

    assert result.output == "two tools complete"
    assert result.budget.used.requests == 3
    assert result.budget.used.tool_calls == 2


def _final_window_budget() -> Budget:
    return Budget(
        max_requests=20,
        max_input_tokens=100_000,
        max_output_tokens=10_000,
        max_total_tokens=110_000,
        max_tool_calls=40,
        max_duration_seconds=60,
        max_cost=None,
        max_children=0,
        max_depth=0,
    )


def _exploration_steps() -> tuple[FakeToolCallStep, ...]:
    return tuple(
        FakeToolCallStep(
            tool_name="workspace.list",
            arguments={"path": "."},
            tool_call_id=f"explore-{index}",
        )
        for index in range(17)
    )


@pytest.mark.asyncio
async def test_final_window_retries_invalid_tool_output_then_completes(
    database: Database,
    tmp_path: Path,
) -> None:
    service, providers = _runtime(
        database,
        FakeScript(
            steps=(
                *_exploration_steps(),
                FakeToolCallStep(
                    tool_name="workspace.list",
                    arguments={"path": "."},
                    tool_call_id="hidden-tool-retry",
                ),
                FakeTextStep(text="final answer"),
            )
        ),
        tool_names=("workspace.list",),
        max_steps=20,
    )
    try:
        result = await service.run(
            RunRequest(
                prompt="inspect then summarize",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
                budget=_final_window_budget(),
            )
        )
    finally:
        await providers.aclose()

    assert result.output == "final answer"
    assert result.budget.used.requests == 19
    assert result.budget.used.tool_calls == 17


@pytest.mark.asyncio
async def test_exhausted_final_output_retries_are_classified(
    database: Database,
    tmp_path: Path,
) -> None:
    conversation_id = uuid4()
    invalid_final_steps = tuple(
        FakeToolCallStep(
            tool_name="workspace.list",
            arguments={"path": "."},
            tool_call_id=f"hidden-tool-{index}",
        )
        for index in range(3)
    )
    service, providers = _runtime(
        database,
        FakeScript(steps=(*_exploration_steps(), *invalid_final_steps)),
        tool_names=("workspace.list",),
        max_steps=20,
    )
    try:
        with pytest.raises(ModelOutputError, match="after 3 attempts"):
            await service.run(
                RunRequest(
                    prompt="inspect then summarize",
                    workspace=tmp_path,
                    conversation_id=conversation_id,
                    lease=CapabilityLease(filesystem_read=(".",)),
                    budget=_final_window_budget(),
                )
            )
    finally:
        await providers.aclose()

    async with database.session() as session:
        row = await session.scalar(select(RunRow).where(RunRow.conversation_id == conversation_id))
        assert row is not None
        events = await EventStore(session).list_for_run(row.id)
    assert row.status == "failed"
    assert events[-1].event_type is EventType.RUN_FAILED
    assert isinstance(events[-1].payload, ErrorPayload)
    assert events[-1].payload.code == "model_output_invalid"


@pytest.mark.asyncio
async def test_tool_failure_is_persisted_before_run_failure(
    database: Database,
    tmp_path: Path,
) -> None:
    conversation_id = uuid4()
    service, providers = _runtime(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="workspace.read",
                    arguments={"path": "missing.txt"},
                ),
            )
        ),
        tool_names=("workspace.read",),
    )
    try:
        with pytest.raises(WorkspacePathNotFoundError):
            await service.run(
                RunRequest(
                    prompt="read missing",
                    workspace=tmp_path,
                    conversation_id=conversation_id,
                    lease=CapabilityLease(filesystem_read=(".",)),
                )
            )
    finally:
        await providers.aclose()

    async with database.session() as session:
        row = await session.scalar(select(RunRow).where(RunRow.conversation_id == conversation_id))
        assert row is not None
        events = await EventStore(session).list_for_run(row.id)
    assert row.status == "failed"
    assert EventType.TOOL_FAILED in [event.event_type for event in events]
    assert events[-1].event_type is EventType.RUN_FAILED


@pytest.mark.asyncio
async def test_provider_failure_persists_failed_terminal_event(
    database: Database,
    tmp_path: Path,
) -> None:
    conversation_id = uuid4()
    service, providers = _runtime(
        database,
        FakeScript(steps=(FakeFailureStep(failure=FakeFailureKind.AUTHENTICATION),)),
    )
    try:
        with pytest.raises(ProviderAuthenticationError):
            await service.run(
                RunRequest(
                    prompt="fail",
                    workspace=tmp_path,
                    conversation_id=conversation_id,
                )
            )
    finally:
        await providers.aclose()

    async with database.session() as session:
        row = await session.scalar(select(RunRow).where(RunRow.conversation_id == conversation_id))
        assert row is not None
        events = await EventStore(session).list_for_run(row.id)
    assert row.status == "failed"
    assert [event.event_type for event in events][-3:] == [
        EventType.MODEL_FAILED,
        EventType.RUN_STATUS_CHANGED,
        EventType.RUN_FAILED,
    ]


@pytest.mark.asyncio
async def test_request_budget_failure_never_calls_model(
    database: Database,
    tmp_path: Path,
) -> None:
    conversation_id = uuid4()
    service, providers = _runtime(
        database,
        FakeScript(steps=(FakeTextStep(text="must not run"),)),
    )
    budget = Budget(
        max_requests=0,
        max_input_tokens=100,
        max_output_tokens=100,
        max_total_tokens=200,
        max_tool_calls=0,
        max_duration_seconds=30,
        max_cost=None,
        max_children=0,
        max_depth=0,
    )
    try:
        with pytest.raises(BudgetExceededError):
            await service.run(
                RunRequest(
                    prompt="do not call",
                    workspace=tmp_path,
                    conversation_id=conversation_id,
                    budget=budget,
                )
            )
    finally:
        await providers.aclose()

    async with database.session() as session:
        row = await session.scalar(select(RunRow).where(RunRow.conversation_id == conversation_id))
        assert row is not None
        events = await EventStore(session).list_for_run(row.id)
    assert row.status == "failed"
    assert EventType.MODEL_REQUESTED not in [event.event_type for event in events]
    assert events[-1].event_type is EventType.RUN_FAILED


@pytest.mark.asyncio
async def test_cancellation_persists_cancelled_terminal_state(
    database: Database,
    tmp_path: Path,
) -> None:
    class SlowParams(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

    async def slow(params: SlowParams, context: ToolExecutionContext) -> str:
        del params, context
        await asyncio.sleep(30)
        return "late"

    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            name="test.slow",
            description="Wait until cancellation.",
            params_model=SlowParams,
            policy=ToolPolicy(
                risk=RiskLevel.SAFE,
                requires_approval=False,
                idempotent=True,
                timeout_seconds=60,
                max_output_bytes=100,
                capabilities=frozenset({Capability.FILESYSTEM_READ}),
            ),
            handler=slow,
        )
    )
    model = FakeModelSpec(model="cancel-script")
    providers = ProviderFactory(
        {"fake_cancel": model},
        adapters=[
            FakeProviderAdapter(
                {
                    model.model: FakeScript(
                        steps=(
                            FakeToolCallStep(tool_name="test.slow"),
                            FakeTextStep(text="never reached"),
                        )
                    )
                }
            )
        ],
    )
    agent = AgentSpec(
        id="coordinator",
        name="Coordinator",
        description="Cancellation test.",
        model_ref="fake_cancel",
        instructions="Call the tool.",
        tools=("test.slow",),
        capabilities=frozenset({Capability.FILESYSTEM_READ}),
    )
    service = RunService(
        database=database,
        providers=providers,
        agents=AgentRegistry([agent]),
        tools=tools,
    )
    conversation_id = uuid4()
    task = asyncio.create_task(
        service.run(
            RunRequest(
                prompt="wait",
                workspace=tmp_path,
                conversation_id=conversation_id,
                lease=CapabilityLease(filesystem_read=(".",)),
            )
        )
    )
    try:
        for _ in range(100):
            async with database.session() as session:
                row = await session.scalar(
                    select(RunRow).where(RunRow.conversation_id == conversation_id)
                )
                if row is not None:
                    events = await EventStore(session).list_for_run(row.id)
                    if any(event.event_type is EventType.TOOL_STARTED for event in events):
                        break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("slow tool did not start")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await providers.aclose()

    async with database.session() as session:
        row = await session.scalar(select(RunRow).where(RunRow.conversation_id == conversation_id))
        assert row is not None
        events = await EventStore(session).list_for_run(row.id)
    assert row.status == "cancelled"
    assert events[-1].event_type is EventType.RUN_CANCELLED
