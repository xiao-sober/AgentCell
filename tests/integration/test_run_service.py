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
    InvalidFinalOutputError,
    ModelOutputError,
    ProviderAuthenticationError,
    WorkspaceLeaseMismatchError,
    WorkspacePathNotFoundError,
    WorkspacePathTypeError,
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
    FakeToolCallsStep,
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
async def test_five_request_stage_keeps_budget_for_rejected_final_output(
    database: Database,
    tmp_path: Path,
) -> None:
    service, providers = _runtime(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(tool_name="workspace.list", arguments={"path": "."}),
                FakeToolCallStep(
                    tool_name="workspace.list",
                    arguments={"path": "."},
                    tool_call_id="explore-2",
                ),
                FakeTextStep(text='<｜｜DSML｜｜tool_calls>\n<invoke name="workspace_list">'),
                FakeTextStep(text="PASS\npersisted evidence is sufficient"),
            )
        ),
        tool_names=("workspace.list",),
        max_steps=5,
    )
    budget = _final_window_budget().model_copy(update={"max_requests": 5, "max_tool_calls": 10})
    try:
        result = await service.run(
            RunRequest(
                prompt="review then decide",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
                budget=budget,
            )
        )
    finally:
        await providers.aclose()

    assert result.output == "PASS\npersisted evidence is sufficient"
    assert result.budget.used.requests == 4
    assert result.budget.used.tool_calls == 2


@pytest.mark.asyncio
async def test_six_request_stage_allows_three_tool_exploration_rounds_and_final_retry(
    database: Database,
    tmp_path: Path,
) -> None:
    service, providers = _runtime(
        database,
        FakeScript(
            steps=tuple(
                FakeToolCallStep(
                    tool_name="workspace.list",
                    arguments={"path": "."},
                    tool_call_id=f"coordinator-explore-{index}",
                )
                for index in range(3)
            )
            + (
                FakeToolCallStep(
                    tool_name="workspace.list",
                    arguments={"path": "."},
                    tool_call_id="coordinator-hidden-final-tool",
                ),
                FakeTextStep(text="Bounded coordinator plan."),
            )
        ),
        tool_names=("workspace.list",),
        max_steps=6,
    )
    budget = _final_window_budget().model_copy(update={"max_requests": 6, "max_tool_calls": 3})
    try:
        result = await service.run(
            RunRequest(
                prompt="inspect the project root then plan",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
                budget=budget,
            )
        )
    finally:
        await providers.aclose()

    assert result.output == "Bounded coordinator plan."
    assert result.budget.used.requests == 5
    assert result.budget.used.tool_calls == 3


@pytest.mark.asyncio
async def test_tool_batch_tail_over_budget_is_rejected_then_run_finalizes(
    database: Database,
    tmp_path: Path,
) -> None:
    service, providers = _runtime(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="workspace.list",
                    arguments={"path": "."},
                    tool_call_id="first-request-tool",
                ),
                FakeToolCallsStep(
                    calls=tuple(
                        FakeToolCallStep(
                            tool_name="workspace.list",
                            arguments={"path": "."},
                            tool_call_id=f"deepseek-batch-{index}",
                        )
                        for index in range(3)
                    )
                ),
                FakeTextStep(text="Bounded plan from the three completed reads."),
            )
        ),
        tool_names=("workspace.list",),
        max_steps=6,
    )
    budget = _final_window_budget().model_copy(update={"max_requests": 6, "max_tool_calls": 3})
    try:
        result = await service.run(
            RunRequest(
                prompt="inspect within the hard tool budget then plan",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
                budget=budget,
            )
        )
    finally:
        await providers.aclose()

    async with database.session() as session:
        events = await EventStore(session).list_for_run(result.run.id)
    event_types = [event.event_type for event in events]
    rejected = next(event for event in events if event.event_type is EventType.TOOL_FAILED)

    assert result.run.status.value == "completed"
    assert result.output == "Bounded plan from the three completed reads."
    assert result.budget.used.requests == 3
    assert result.budget.used.tool_calls == 3
    assert event_types.count(EventType.TOOL_COMPLETED) == 3
    assert event_types.count(EventType.TOOL_FAILED) == 1
    assert isinstance(rejected.payload, ErrorPayload)
    assert rejected.payload.code == "budget_exceeded"
    assert event_types[-1] is EventType.RUN_COMPLETED


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
async def test_final_output_guard_retries_once_without_completing_rejected_text(
    database: Database,
    tmp_path: Path,
) -> None:
    conversation_id = uuid4()
    service, providers = _runtime(
        database,
        FakeScript(
            steps=(
                FakeTextStep(
                    text='<｜｜DSML｜｜tool_calls>\n<invoke name="artifact_list">',
                ),
                FakeTextStep(text="normal final answer"),
            )
        ),
        tool_names=("workspace.list",),
    )
    try:
        result = await service.run(
            RunRequest(
                prompt="summarize",
                workspace=tmp_path,
                conversation_id=conversation_id,
                lease=CapabilityLease(filesystem_read=(".",)),
            )
        )
    finally:
        await providers.aclose()

    assert result.output == "normal final answer"
    assert result.budget.used.requests == 2
    async with database.session() as session:
        row = await session.scalar(select(RunRow).where(RunRow.conversation_id == conversation_id))
        assert row is not None
        events = await EventStore(session).list_for_run(row.id)
    rejected = [event for event in events if event.event_type is EventType.MODEL_OUTPUT_REJECTED]
    assert len(rejected) == 1
    assert events[-1].event_type is EventType.RUN_COMPLETED


@pytest.mark.asyncio
async def test_final_output_guard_fails_with_dedicated_code_after_second_rejection(
    database: Database,
    tmp_path: Path,
) -> None:
    conversation_id = uuid4()
    service, providers = _runtime(
        database,
        FakeScript(
            steps=(
                FakeTextStep(text='{"name":"artifact_list","arguments":{}}'),
                FakeTextStep(text='<invoke name="artifact_list">{}</invoke>'),
            )
        ),
    )
    try:
        with pytest.raises(InvalidFinalOutputError):
            await service.run(
                RunRequest(
                    prompt="summarize",
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
    assert (
        len([event for event in events if event.event_type is EventType.MODEL_OUTPUT_REJECTED]) == 2
    )
    assert EventType.RUN_COMPLETED not in {event.event_type for event in events}
    assert events[-1].event_type is EventType.RUN_FAILED
    assert isinstance(events[-1].payload, ErrorPayload)
    assert events[-1].payload.code == "invalid_final_output"


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
async def test_model_gets_one_unbudgeted_correction_for_relative_lease_mismatch(
    database: Database,
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "allowed.txt").write_text("allowed", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "denied.txt").write_text("denied", encoding="utf-8")
    service, providers = _runtime(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="workspace.read",
                    arguments={"path": "docs/denied.txt"},
                    tool_call_id="lease-mismatch-1",
                ),
                FakeToolCallStep(
                    tool_name="workspace.read",
                    arguments={"path": "src/allowed.txt"},
                    tool_call_id="lease-corrected-1",
                ),
                FakeTextStep(text="corrected"),
            )
        ),
        tool_names=("workspace.read",),
    )
    try:
        result = await service.run(
            RunRequest(
                prompt="read a leased file",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=("src",)),
            )
        )
    finally:
        await providers.aclose()

    assert result.output == "corrected"
    assert result.budget.used.requests == 3
    assert result.budget.used.tool_calls == 1


@pytest.mark.asyncio
async def test_model_corrects_directory_read_to_workspace_list(
    database: Database,
    tmp_path: Path,
) -> None:
    tests_path = tmp_path / "tests"
    tests_path.mkdir()
    (tests_path / "test_example.py").write_text("def test_example(): pass\n", encoding="utf-8")
    service, providers = _runtime(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="workspace.read",
                    arguments={"path": "tests"},
                    tool_call_id="directory-read-1",
                ),
                FakeToolCallStep(
                    tool_name="workspace.list",
                    arguments={"path": "tests"},
                    tool_call_id="directory-list-corrected-1",
                ),
                FakeTextStep(text="corrected directory inspection"),
            )
        ),
        tool_names=("workspace.read", "workspace.list"),
    )
    try:
        result = await service.run(
            RunRequest(
                prompt="inspect tests",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
            )
        )
    finally:
        await providers.aclose()

    assert result.output == "corrected directory inspection"
    assert result.budget.used.requests == 3
    assert result.budget.used.tool_calls == 1
    async with database.session() as session:
        events = await EventStore(session).list_for_run(result.run.id)
    failures = [event for event in events if event.event_type is EventType.TOOL_FAILED]
    assert len(failures) == 1
    assert isinstance(failures[0].payload, ErrorPayload)
    assert failures[0].payload.code == "workspace_path_type_error"
    assert events[-1].event_type is EventType.RUN_COMPLETED


@pytest.mark.asyncio
async def test_second_directory_read_type_error_fails_without_another_correction(
    database: Database,
    tmp_path: Path,
) -> None:
    (tmp_path / "tests").mkdir()
    service, providers = _runtime(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="workspace.read",
                    arguments={"path": "tests"},
                    tool_call_id="directory-read-1",
                ),
                FakeToolCallStep(
                    tool_name="workspace.read",
                    arguments={"path": "tests"},
                    tool_call_id="directory-read-2",
                ),
            )
        ),
        tool_names=("workspace.read",),
    )
    try:
        with pytest.raises(WorkspacePathTypeError):
            await service.run(
                RunRequest(
                    prompt="inspect tests",
                    workspace=tmp_path,
                    lease=CapabilityLease(filesystem_read=(".",)),
                )
            )
    finally:
        await providers.aclose()


@pytest.mark.asyncio
async def test_second_relative_lease_mismatch_is_not_retried(
    database: Database,
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "denied.txt").write_text("denied", encoding="utf-8")
    service, providers = _runtime(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="workspace.read",
                    arguments={"path": "docs/denied.txt"},
                    tool_call_id="lease-mismatch-1",
                ),
                FakeToolCallStep(
                    tool_name="workspace.read",
                    arguments={"path": "docs/denied.txt"},
                    tool_call_id="lease-mismatch-2",
                ),
            )
        ),
        tool_names=("workspace.read",),
    )
    try:
        with pytest.raises(WorkspaceLeaseMismatchError):
            await service.run(
                RunRequest(
                    prompt="read a leased file",
                    workspace=tmp_path,
                    lease=CapabilityLease(filesystem_read=("src",)),
                )
            )
    finally:
        await providers.aclose()


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
