"""Stage 9.4.2 authoritative task roots, route events, and safe validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcell.agents import (
    AgentRegistry,
    AgentVisibility,
    TeamRegistry,
    coder_spec,
    coordinator_spec,
    finalizer_spec,
    researcher_spec,
    reviewer_spec,
    software_team_spec,
)
from agentcell.application import build_application
from agentcell.display import RunDisplayProjector
from agentcell.events import ErrorPayload, EventType, TaskRouteEventPayload
from agentcell.kernel import RunStatus
from agentcell.kernel.checkpoint import CheckpointKind
from agentcell.kernel.handoff import HandoffService
from agentcell.kernel.replay import ReplayService
from agentcell.kernel.run_service import RunService
from agentcell.policy import ApprovalDecision, ApprovalDecisionKind, CapabilityLease
from agentcell.providers import (
    FakeModelSpec,
    FakeProviderAdapter,
    FakeScript,
    FakeTextStep,
    FakeToolCallStep,
    ProviderFactory,
)
from agentcell.routing import (
    TASK_ROUTER_AGENT_ID,
    TaskRouteIssueCode,
    TaskRouteRequest,
    TaskRouteSource,
    TaskRouteStatus,
    TaskRoutingService,
)
from agentcell.storage import (
    AgentDelegationRepository,
    CheckpointRepository,
    Database,
    EventStore,
    RunRepository,
)
from agentcell.tools import ToolRegistry, register_shell_tools, register_workspace_tools


def _service(
    database: Database,
    *,
    model_ref: str = "fake",
    configured_model_ref: str = "fake",
) -> tuple[TaskRoutingService, ProviderFactory, TeamRegistry]:
    agents = AgentRegistry()
    for spec in (
        coordinator_spec(model_ref=model_ref, collaborative=False),
        coder_spec(model_ref=model_ref),
        reviewer_spec(model_ref=model_ref),
        researcher_spec(model_ref=model_ref),
    ):
        agents.register(spec, visibility=AgentVisibility.PUBLIC)
    agents.register(
        finalizer_spec(model_ref=model_ref),
        visibility=AgentVisibility.INTERNAL,
    )
    teams = TeamRegistry((software_team_spec(model_ref=model_ref),))
    providers = ProviderFactory({configured_model_ref: FakeModelSpec(model="routing-fake")})
    return (
        TaskRoutingService(
            database=database,
            agents=agents,
            teams=teams,
            providers=providers,
        ),
        providers,
        teams,
    )


@pytest.mark.asyncio
async def test_authoritative_ready_route_creates_root_before_confirmed_events(
    database: Database,
    tmp_path: Path,
) -> None:
    service, providers, teams = _service(database)
    request = TaskRouteRequest(
        task="分析项目结构并给出规划",
        workspace=tmp_path,
        lease=CapabilityLease(filesystem_read=(".",)),
        budget=teams.get("software").default_budget,
    )
    try:
        prepared = await service.prepare(request)
    finally:
        await providers.aclose()

    async with database.session() as session:
        persisted = await RunRepository(session).get(prepared.root.id)
        events = await EventStore(session).list_for_run(prepared.root.id)

    assert persisted is not None
    assert persisted.status is RunStatus.CREATED
    assert persisted.agent_id == TASK_ROUTER_AGENT_ID
    assert persisted.execution_identity is None
    assert prepared.decision.status is TaskRouteStatus.READY
    assert [event.event_type for event in events] == [
        EventType.RUN_STARTED,
        EventType.TASK_ROUTE_PROPOSED,
        EventType.TASK_ROUTE_CONFIRMED,
        EventType.CHECKPOINT_CREATED,
    ]
    assert isinstance(events[1].payload, TaskRouteEventPayload)
    assert events[1].payload.task_sha256 == request.task_sha256
    assert request.task not in str(events[1].payload.safe_dump())
    replayed = await ReplayService(database).replay(prepared.root.id)
    assert replayed.status is RunStatus.CREATED


@pytest.mark.asyncio
async def test_ambiguous_route_uses_bounded_model_and_accounts_usage_on_root(
    database: Database,
    tmp_path: Path,
) -> None:
    agents = AgentRegistry()
    for spec in (
        coordinator_spec(model_ref="fake", collaborative=False),
        coder_spec(model_ref="fake"),
        reviewer_spec(model_ref="fake"),
        researcher_spec(model_ref="fake"),
    ):
        agents.register(spec, visibility=AgentVisibility.PUBLIC)
    agents.register(finalizer_spec(model_ref="fake"), visibility=AgentVisibility.INTERNAL)
    teams = TeamRegistry((software_team_spec(model_ref="fake"),))
    model = FakeModelSpec(model="routing-classifier")
    providers = ProviderFactory(
        {"fake": model},
        adapters=(
            FakeProviderAdapter(
                {
                    model.model: FakeScript(
                        steps=(
                            FakeTextStep(
                                text=(
                                    '{"target_id":"reviewer","confidence":0.91,'
                                    '"reason_summary":"需要只读独立审查",'
                                    '"requires_clarification":false}'
                                )
                            ),
                        )
                    )
                }
            ),
        ),
    )
    service = TaskRoutingService(
        database=database,
        agents=agents,
        teams=teams,
        providers=providers,
        routing_model_ref="fake",
    )
    try:
        prepared = await service.prepare(
            TaskRouteRequest(
                task="帮我看看这个",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
                budget=teams.get("software").default_budget,
            )
        )
    finally:
        await providers.aclose()

    async with database.session() as session:
        events = await EventStore(session).list_for_run(prepared.root.id)

    assert prepared.decision.source is TaskRouteSource.MODEL
    assert prepared.decision.agent_id == "reviewer"
    assert prepared.decision.status is TaskRouteStatus.READY
    assert prepared.decision.routing_usage.requests == 1
    assert EventType.MODEL_REQUESTED in [event.event_type for event in events]
    assert EventType.MODEL_COMPLETED in [event.event_type for event in events]
    assert EventType.BUDGET_UPDATED in [event.event_type for event in events]


@pytest.mark.asyncio
async def test_invalid_model_route_fails_closed_to_confirmed_coordinator(
    database: Database,
    tmp_path: Path,
) -> None:
    agents = AgentRegistry()
    for spec in (
        coordinator_spec(model_ref="fake", collaborative=False),
        coder_spec(model_ref="fake"),
        reviewer_spec(model_ref="fake"),
        researcher_spec(model_ref="fake"),
    ):
        agents.register(spec, visibility=AgentVisibility.PUBLIC)
    agents.register(finalizer_spec(model_ref="fake"), visibility=AgentVisibility.INTERNAL)
    teams = TeamRegistry((software_team_spec(model_ref="fake"),))
    model = FakeModelSpec(model="invalid-routing-classifier")
    providers = ProviderFactory(
        {"fake": model},
        adapters=(
            FakeProviderAdapter(
                {model.model: FakeScript(steps=(FakeTextStep(text="not structured"),))}
            ),
        ),
    )
    service = TaskRoutingService(
        database=database,
        agents=agents,
        teams=teams,
        providers=providers,
        routing_model_ref="fake",
    )
    try:
        prepared = await service.prepare(
            TaskRouteRequest(
                task="帮我看看这个",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
                budget=teams.get("software").default_budget,
            )
        )
    finally:
        await providers.aclose()

    assert prepared.decision.source is TaskRouteSource.SAFE_FALLBACK
    assert prepared.decision.agent_id == "coordinator"
    assert prepared.decision.status is TaskRouteStatus.CONFIRMATION_REQUIRED
    assert TaskRouteIssueCode.MODEL_FALLBACK_FAILED in {
        issue.code for issue in prepared.decision.issues
    }


@pytest.mark.asyncio
async def test_capability_gap_pauses_root_without_expanding_lease(
    database: Database,
    tmp_path: Path,
) -> None:
    service, providers, teams = _service(database)
    lease = CapabilityLease(filesystem_read=(".",))
    try:
        prepared = await service.prepare(
            TaskRouteRequest(
                task="修复测试并独立审查",
                workspace=tmp_path,
                lease=lease,
                budget=teams.get("software").default_budget,
            )
        )
    finally:
        await providers.aclose()

    async with database.session() as session:
        events = await EventStore(session).list_for_run(prepared.root.id)

    assert prepared.root.status is RunStatus.PAUSED
    assert prepared.decision.status is TaskRouteStatus.CONFIRMATION_REQUIRED
    assert lease == CapabilityLease(filesystem_read=(".",))
    assert [event.event_type for event in events] == [
        EventType.RUN_STARTED,
        EventType.RUN_STATUS_CHANGED,
        EventType.TASK_ROUTE_PROPOSED,
        EventType.RUN_STATUS_CHANGED,
        EventType.CHECKPOINT_CREATED,
    ]
    replayed = await ReplayService(database).replay(prepared.root.id)
    assert replayed.status is RunStatus.PAUSED


@pytest.mark.asyncio
async def test_explicit_override_is_validated_and_audited(
    database: Database,
    tmp_path: Path,
) -> None:
    service, providers, teams = _service(database)
    try:
        prepared = await service.prepare(
            TaskRouteRequest(
                task="独立审查当前修改",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
                budget=teams.get("software").default_budget,
                agent_id="reviewer",
            )
        )
    finally:
        await providers.aclose()

    async with database.session() as session:
        events = await EventStore(session).list_for_run(prepared.root.id)

    assert prepared.decision.source is TaskRouteSource.OVERRIDE
    assert prepared.decision.status is TaskRouteStatus.READY
    assert [event.event_type for event in events] == [
        EventType.RUN_STARTED,
        EventType.TASK_ROUTE_PROPOSED,
        EventType.TASK_ROUTE_OVERRIDDEN,
        EventType.TASK_ROUTE_CONFIRMED,
        EventType.CHECKPOINT_CREATED,
    ]


@pytest.mark.asyncio
async def test_invalid_workspace_still_has_a_failed_auditable_root(
    database: Database,
    tmp_path: Path,
) -> None:
    service, providers, teams = _service(database)
    missing = tmp_path / "missing"
    try:
        prepared = await service.prepare(
            TaskRouteRequest(
                task="分析项目",
                workspace=missing,
                lease=CapabilityLease(filesystem_read=(".",)),
                budget=teams.get("software").default_budget,
            )
        )
    finally:
        await providers.aclose()

    async with database.session() as session:
        events = await EventStore(session).list_for_run(prepared.root.id)

    assert prepared.root.status is RunStatus.FAILED
    assert prepared.decision.status is TaskRouteStatus.REJECTED
    assert prepared.decision.issues[0].code is TaskRouteIssueCode.WORKSPACE_INVALID
    assert events[0].event_type is EventType.RUN_STARTED
    assert EventType.TASK_ROUTE_REJECTED in [event.event_type for event in events]
    assert events[-2].event_type is EventType.RUN_FAILED
    assert events[-1].event_type is EventType.CHECKPOINT_CREATED
    assert isinstance(events[-2].payload, ErrorPayload)
    assert events[-2].payload.code == TaskRouteIssueCode.WORKSPACE_INVALID.value
    replayed = await ReplayService(database).replay(prepared.root.id)
    assert replayed.status is RunStatus.FAILED


@pytest.mark.asyncio
async def test_unconfigured_provider_and_small_team_budget_are_rejected(
    database: Database,
    tmp_path: Path,
) -> None:
    service, providers, teams = _service(
        database,
        model_ref="missing",
        configured_model_ref="different",
    )
    budget = teams.get("software").default_budget.model_copy(update={"max_requests": 17})
    try:
        prepared = await service.prepare(
            TaskRouteRequest(
                task="修复测试并独立审查",
                workspace=tmp_path,
                lease=CapabilityLease(
                    filesystem_read=(".",),
                    filesystem_write=(".",),
                    commands=frozenset({"pytest"}),
                ),
                budget=budget,
            )
        )
    finally:
        await providers.aclose()

    codes = {issue.code for issue in prepared.decision.issues}
    assert prepared.root.status is RunStatus.FAILED
    assert TaskRouteIssueCode.BUDGET_INSUFFICIENT in codes
    assert TaskRouteIssueCode.PROVIDER_UNAVAILABLE in codes


@pytest.mark.asyncio
async def test_ready_single_agent_route_executes_one_direct_child_and_is_resumable(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    app = await build_application(
        database_url=migrated_database_url,
        offline_fake=True,
        fake_output="analysis complete",
    )
    request = TaskRouteRequest(
        task="分析项目结构并给出规划",
        workspace=tmp_path,
        lease=CapabilityLease(filesystem_read=(".",)),
        budget=app.teams.get("software").default_budget,
    )
    try:
        prepared = await app.routing.prepare(request)
        result = await app.routing.execute(prepared)
        resumed = await app.routing.resume(result.run.id)
        async with app.database.session() as session:
            delegations = await AgentDelegationRepository(session).list_for_parent(result.run.id)
            checkpoint = await CheckpointRepository(session).latest(result.run.id)
            events = await EventStore(session).list_for_run(result.run.id)
    finally:
        await app.close()

    assert result.run.status is RunStatus.COMPLETED
    assert result.output == "analysis complete"
    assert len(delegations) == 1
    assert delegations[0].target_agent_id == "coordinator"
    assert delegations[0].child_run_id in result.child_run_ids
    assert result.budget.used.children == 1
    assert checkpoint.kind is CheckpointKind.TASK_ROUTE
    assert checkpoint.workflow_state is not None
    assert checkpoint.workflow_state["route_state"] == "completed"
    projected = [
        event
        for event in events
        if event.event_type is EventType.MODEL_TEXT_DELTA
        and getattr(event.payload, "source_run_id", None) is not None
    ]
    assert "".join(str(event.safe_payload()["delta"]) for event in projected) == result.output
    assert resumed == result


@pytest.mark.asyncio
async def test_single_agent_route_projects_tool_and_file_activity_to_root(
    database: Database,
    tmp_path: Path,
) -> None:
    target = tmp_path / "README.md"
    target.write_text("project evidence", encoding="utf-8")
    model = FakeModelSpec(model="routed-tool-visibility-fake")
    providers = ProviderFactory(
        {"fake": model},
        adapters=(
            FakeProviderAdapter(
                {
                    model.model: FakeScript(
                        steps=(
                            FakeToolCallStep(
                                tool_name="workspace.read",
                                arguments={"path": "README.md"},
                            ),
                            FakeTextStep(text="inspection complete"),
                        )
                    )
                }
            ),
        ),
    )
    agents = AgentRegistry()
    for spec in (
        coordinator_spec(model_ref="fake", collaborative=False),
        coder_spec(model_ref="fake"),
        reviewer_spec(model_ref="fake"),
        researcher_spec(model_ref="fake"),
    ):
        agents.register(spec, visibility=AgentVisibility.PUBLIC)
    agents.register(finalizer_spec(model_ref="fake"), visibility=AgentVisibility.INTERNAL)
    tools = ToolRegistry()
    register_workspace_tools(tools)
    runs = RunService(database=database, providers=providers, agents=agents, tools=tools)
    teams = TeamRegistry((software_team_spec(model_ref="fake"),))
    service = TaskRoutingService(
        database=database,
        agents=agents,
        teams=teams,
        providers=providers,
        runs=runs,
        handoffs=HandoffService(database, runs),
    )
    try:
        prepared = await service.prepare(
            TaskRouteRequest(
                task="分析 README 文件",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
                budget=teams.get("software").default_budget,
            )
        )
        result = await service.execute(prepared)
        async with database.session() as session:
            events = await EventStore(session).list_for_run(result.run.id)
    finally:
        await providers.aclose()

    tool_events = [
        event
        for event in events
        if event.event_type
        in {EventType.TOOL_PROPOSED, EventType.TOOL_STARTED, EventType.TOOL_COMPLETED}
    ]
    assert [event.event_type for event in tool_events] == [
        EventType.TOOL_PROPOSED,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
    ]
    proposed_data = tool_events[0].safe_payload()["data"]
    assert isinstance(proposed_data, dict)
    assert proposed_data["tool_name"] == "workspace.read"
    assert proposed_data["arguments"] == {"path": "README.md"}
    assert proposed_data["source_run_id"] == str(result.child_run_ids[0])
    completed_data = tool_events[-1].safe_payload()["data"]
    assert isinstance(completed_data, dict)
    assert "output" not in completed_data
    progress: list[tuple[int, int]] = []
    for event in events:
        if event.event_type is not EventType.BUDGET_UPDATED:
            continue
        data = event.safe_payload().get("data")
        if not isinstance(data, dict) or data.get("source") != "child_progress":
            continue
        snapshot = data.get("snapshot")
        assert isinstance(snapshot, dict)
        used = snapshot.get("used")
        assert isinstance(used, dict)
        requests = used.get("requests")
        tool_calls = used.get("tool_calls")
        assert isinstance(requests, int)
        assert isinstance(tool_calls, int)
        progress.append((requests, tool_calls))
        limits = snapshot.get("budget")
        assert isinstance(limits, dict)
        assert limits["max_requests"] == 24
        assert limits["max_tool_calls"] == 48
    assert progress
    assert progress[-1][0] >= 1
    assert progress[-1][1] == 1
    settled = None
    for event in reversed(events):
        if event.event_type is not EventType.BUDGET_UPDATED:
            continue
        candidate = event.safe_payload().get("data")
        if isinstance(candidate, dict) and candidate.get("source") == "task_child_settled":
            settled = candidate
            break
    assert isinstance(settled, dict)
    settled_snapshot = settled.get("snapshot")
    assert isinstance(settled_snapshot, dict)
    settled_used = settled_snapshot.get("used")
    assert isinstance(settled_used, dict)
    settled_requests = settled_used.get("requests")
    settled_tool_calls = settled_used.get("tool_calls")
    assert isinstance(settled_requests, int)
    assert isinstance(settled_tool_calls, int)
    assert settled_requests >= progress[-1][0]
    assert settled_tool_calls == progress[-1][1]

    projector = RunDisplayProjector()
    for event in events:
        projector.apply(event)
    activity = next(
        item for item in projector.state.activities if item.key == "tool:workspace.read"
    )
    assert activity.label == "文件读取完成"
    assert activity.agent_id == "coordinator"
    assert activity.tool_name == "workspace.read"
    assert activity.detail == "README.md"


@pytest.mark.asyncio
async def test_confirmed_capability_upgrade_executes_team_on_same_root(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    app = await build_application(
        database_url=migrated_database_url,
        offline_fake=True,
        fake_output="PASS\noffline stage complete",
    )
    request = TaskRouteRequest(
        task="修复测试并独立审查",
        workspace=tmp_path,
        lease=CapabilityLease(filesystem_read=(".",)),
        budget=app.teams.get("software").default_budget,
    )
    try:
        prepared = await app.routing.prepare(request)
        confirmed = await app.routing.confirm(
            prepared.root.id,
            decision_hash=str(prepared.decision.decision_hash),
            authorized_lease=CapabilityLease(
                filesystem_read=(".",),
                filesystem_write=(".",),
                commands=frozenset({"pytest"}),
            ),
        )
        result = await app.routing.execute(confirmed)
        resumed = await app.routing.resume(result.run.id)
        async with app.database.session() as session:
            delegations = await AgentDelegationRepository(session).list_for_parent(result.run.id)
            events = await EventStore(session).list_for_run(result.run.id)
    finally:
        await app.close()

    assert confirmed.root.id == prepared.root.id
    assert result.run.id == prepared.root.id
    assert result.run.status is RunStatus.COMPLETED
    assert [item.target_agent_id for item in delegations] == [
        "coordinator",
        "coder",
        "reviewer",
        "finalizer",
    ]
    assert result.budget.used.children == 4
    assert sum(event.event_type is EventType.RUN_STARTED for event in events) == 1
    assert EventType.TASK_ROUTE_CONFIRMED in [event.event_type for event in events]
    assert resumed == result


@pytest.mark.asyncio
async def test_single_agent_approval_resumes_child_then_reconciles_task_root(
    database: Database,
    tmp_path: Path,
) -> None:
    model = FakeModelSpec(model="route-approval-fake")
    providers = ProviderFactory(
        {"fake": model},
        adapters=(
            FakeProviderAdapter(
                {
                    model.model: FakeScript(
                        steps=(
                            FakeToolCallStep(
                                tool_name="workspace.write",
                                arguments={"path": "created.txt", "content": "approved"},
                            ),
                            FakeTextStep(text="write complete"),
                        )
                    )
                }
            ),
        ),
    )
    agents = AgentRegistry()
    for spec in (
        coordinator_spec(model_ref="fake", collaborative=False),
        coder_spec(model_ref="fake"),
        reviewer_spec(model_ref="fake"),
        researcher_spec(model_ref="fake"),
    ):
        agents.register(spec, visibility=AgentVisibility.PUBLIC)
    agents.register(finalizer_spec(model_ref="fake"), visibility=AgentVisibility.INTERNAL)
    tools = ToolRegistry()
    register_workspace_tools(tools)
    register_shell_tools(tools)
    runs = RunService(
        database=database,
        providers=providers,
        agents=agents,
        tools=tools,
    )
    handoffs = HandoffService(database, runs)
    teams = TeamRegistry((software_team_spec(model_ref="fake"),))
    service = TaskRoutingService(
        database=database,
        agents=agents,
        teams=teams,
        providers=providers,
        runs=runs,
        handoffs=handoffs,
    )
    request = TaskRouteRequest(
        task="实现一个文件",
        workspace=tmp_path,
        lease=CapabilityLease(
            filesystem_read=(".",),
            filesystem_write=(".",),
        ),
        budget=teams.get("software").default_budget,
        agent_id="coder",
    )
    prepared = await service.prepare(request)
    paused = await service.execute(prepared)
    await providers.aclose()

    assert paused.run.status is RunStatus.PAUSED
    assert len(paused.approvals) == 1
    restarted_providers = ProviderFactory(
        {"fake": model},
        adapters=(
            FakeProviderAdapter(
                {model.model: FakeScript(steps=(FakeTextStep(text="write complete"),))}
            ),
        ),
    )
    restarted_runs = RunService(
        database=database,
        providers=restarted_providers,
        agents=agents,
        tools=tools,
    )
    restarted_handoffs = HandoffService(database, restarted_runs)
    restarted = TaskRoutingService(
        database=database,
        agents=agents,
        teams=teams,
        providers=restarted_providers,
        runs=restarted_runs,
        handoffs=restarted_handoffs,
    )
    try:
        completed = await restarted.decide_approval(
            paused.run.id,
            paused.approvals[0].id,
            ApprovalDecision(kind=ApprovalDecisionKind.APPROVE),
        )
    finally:
        await restarted_providers.aclose()

    assert completed.run.status is RunStatus.COMPLETED
    assert completed.output == "write complete"
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "approved"
    assert completed.budget.used.children == 1
    async with database.session() as session:
        root_events = await EventStore(session).list_for_run(completed.run.id)
    projected_tool_events = [
        event
        for event in root_events
        if event.event_type
        in {
            EventType.TOOL_PROPOSED,
            EventType.TOOL_APPROVAL_REQUIRED,
            EventType.TOOL_APPROVED,
            EventType.TOOL_STARTED,
            EventType.TOOL_COMPLETED,
        }
    ]
    source_sequences: list[int] = []
    for event in projected_tool_events:
        data = event.safe_payload().get("data")
        assert isinstance(data, dict)
        source_sequence = data.get("source_sequence")
        assert isinstance(source_sequence, int)
        source_sequences.append(source_sequence)
    assert len(source_sequences) == len(set(source_sequences))
    assert EventType.TOOL_APPROVAL_REQUIRED in {
        event.event_type for event in projected_tool_events
    }
    assert EventType.TOOL_COMPLETED in {event.event_type for event in projected_tool_events}
