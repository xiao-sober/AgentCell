"""Stage 9.4.1 versioned routing contracts and deterministic intent rules."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

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
    summarizer_spec,
)
from agentcell.policy import Capability, CapabilityLease
from agentcell.providers import FakeModelSpec, ProviderFactory
from agentcell.routing import (
    RouteBudgetProfile,
    RoutingPolicy,
    TaskRouteDecision,
    TaskRouteIssueCode,
    TaskRouteMode,
    TaskRouteRequest,
    TaskRouteSource,
    TaskRouteStatus,
    TaskRoutingService,
    deterministic_route,
    is_direct_conversation,
)
from agentcell.storage import Database


def _registries() -> tuple[AgentRegistry, TeamRegistry]:
    agents = AgentRegistry()
    for spec in (
        coordinator_spec(model_ref="fake", collaborative=False),
        coder_spec(model_ref="fake"),
        reviewer_spec(model_ref="fake"),
        researcher_spec(model_ref="fake"),
    ):
        agents.register(spec, visibility=AgentVisibility.PUBLIC)
    for spec in (
        finalizer_spec(model_ref="fake"),
        summarizer_spec(model_ref="fake"),
    ):
        agents.register(spec, visibility=AgentVisibility.INTERNAL)
    return agents, TeamRegistry((software_team_spec(model_ref="fake"),))


@pytest.mark.parametrize(
    ("task", "mode", "target", "profile"),
    [
        (
            "分析项目结构并给出规划",
            TaskRouteMode.SINGLE_AGENT,
            "coordinator",
            RouteBudgetProfile.READ_ONLY,
        ),
        (
            "修复登录模块中的错误",
            TaskRouteMode.SINGLE_AGENT,
            "coder",
            RouteBudgetProfile.CHANGE,
        ),
        (
            "修复测试并独立审查",
            TaskRouteMode.TEAM,
            "software",
            RouteBudgetProfile.DELIVERY,
        ),
        (
            "独立审查当前修改并分析回归风险",
            TaskRouteMode.SINGLE_AGENT,
            "reviewer",
            RouteBudgetProfile.REVIEW,
        ),
        (
            "查找最新官方文档并整理证据",
            TaskRouteMode.SINGLE_AGENT,
            "researcher",
            RouteBudgetProfile.RESEARCH,
        ),
        (
            "分析测试目录结构",
            TaskRouteMode.SINGLE_AGENT,
            "coordinator",
            RouteBudgetProfile.READ_ONLY,
        ),
        (
            "实现新功能并修复测试",
            TaskRouteMode.SINGLE_AGENT,
            "coder",
            RouteBudgetProfile.CHANGE,
        ),
    ],
)
def test_deterministic_route_matrix(
    task: str,
    mode: TaskRouteMode,
    target: str,
    profile: RouteBudgetProfile,
) -> None:
    match = deterministic_route(task)

    assert match.mode is mode
    assert match.target_id == target
    assert match.budget_profile is profile
    assert match.ambiguous is False


def test_ambiguous_task_returns_confirmation_only_safe_fallback() -> None:
    match = deterministic_route("帮我处理一下")

    assert match.mode is TaskRouteMode.SINGLE_AGENT
    assert match.target_id == "coordinator"
    assert match.ambiguous is True
    assert match.confidence < 0.8
    assert match.required_capabilities == frozenset({Capability.FILESYSTEM_READ})


@pytest.mark.parametrize("task", ["你是谁？", "你好", "team是什么意思？", "What is Python?"])
def test_ordinary_questions_use_direct_conversation(task: str) -> None:
    assert is_direct_conversation(task) is True


@pytest.mark.parametrize(
    "task",
    ["分析当前项目", "遍历代码库", "修复这个错误", "帮我看看这个"],
)
def test_workspace_or_ambiguous_work_does_not_use_direct_conversation(task: str) -> None:
    assert is_direct_conversation(task) is False


def test_task_route_request_rejects_conflicting_overrides() -> None:
    team = software_team_spec(model_ref="fake")

    with pytest.raises(ValidationError, match="mutually exclusive"):
        TaskRouteRequest(
            task="analyze",
            workspace=Path("."),
            lease=CapabilityLease(filesystem_read=(".",)),
            budget=team.default_budget,
            agent_id="coordinator",
            team_id="software",
        )


def test_decision_hash_is_stable_and_rejects_tampering() -> None:
    values = {
        "policy_version": "9.4.1-v1",
        "mode": TaskRouteMode.SINGLE_AGENT,
        "agent_id": "coordinator",
        "source": TaskRouteSource.DETERMINISTIC,
        "status": TaskRouteStatus.READY,
        "confidence": 0.93,
        "reason_summary": "read-only analysis",
        "required_capabilities": frozenset({Capability.FILESYSTEM_READ}),
        "budget_profile": RouteBudgetProfile.READ_ONLY,
    }
    first = TaskRouteDecision.model_validate(values)
    second = TaskRouteDecision.model_validate(values)

    assert first.decision_hash == second.decision_hash
    with pytest.raises(ValidationError, match="decision_hash"):
        TaskRouteDecision.model_validate({**values, "decision_hash": "0" * 64})


@pytest.mark.asyncio
async def test_preview_reports_capability_gaps_without_mutating_lease(tmp_path: Path) -> None:
    agents, teams = _registries()
    providers = ProviderFactory({"fake": FakeModelSpec(model="routing-fake")})
    database = Database.from_path(tmp_path / "preview.db")
    service = TaskRoutingService(
        database=database,
        agents=agents,
        teams=teams,
        providers=providers,
    )
    lease = CapabilityLease(filesystem_read=(".",))
    try:
        decision = await service.preview(
            TaskRouteRequest(
                task="修复测试并独立审查",
                workspace=tmp_path,
                lease=lease,
                budget=teams.get("software").default_budget,
            )
        )
    finally:
        await providers.aclose()
        await database.dispose()

    assert decision.status is TaskRouteStatus.CONFIRMATION_REQUIRED
    assert decision.requires_confirmation is True
    assert decision.required_capabilities == frozenset(
        {
            Capability.FILESYSTEM_READ,
            Capability.FILESYSTEM_WRITE,
            Capability.SHELL_EXECUTE,
        }
    )
    assert decision.capability_gaps == frozenset(
        {Capability.FILESYSTEM_WRITE, Capability.SHELL_EXECUTE}
    )
    assert lease == CapabilityLease(filesystem_read=(".",))
    assert {issue.code for issue in decision.issues} == {TaskRouteIssueCode.CAPABILITY_MISSING}


@pytest.mark.asyncio
async def test_internal_agent_override_is_rejected_even_when_policy_names_it(
    tmp_path: Path,
) -> None:
    agents, teams = _registries()
    providers = ProviderFactory({"fake": FakeModelSpec(model="routing-fake")})
    database = Database.from_path(tmp_path / "preview.db")
    service = TaskRoutingService(
        database=database,
        agents=agents,
        teams=teams,
        providers=providers,
        policy=RoutingPolicy(
            public_agent_ids=frozenset(
                {"coordinator", "coder", "reviewer", "researcher", "summarizer"}
            )
        ),
    )
    try:
        decision = await service.preview(
            TaskRouteRequest(
                task="总结",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
                budget=teams.get("software").default_budget,
                agent_id="summarizer",
            )
        )
    finally:
        await providers.aclose()
        await database.dispose()

    assert decision.status is TaskRouteStatus.REJECTED
    assert TaskRouteIssueCode.TARGET_NOT_PUBLIC in {issue.code for issue in decision.issues}


@pytest.mark.asyncio
async def test_ambiguous_preview_requires_confirmation_and_creates_no_database(
    tmp_path: Path,
) -> None:
    agents, teams = _registries()
    providers = ProviderFactory({"fake": FakeModelSpec(model="routing-fake")})
    database_path = tmp_path / "preview.db"
    database = Database.from_path(database_path)
    service = TaskRoutingService(
        database=database,
        agents=agents,
        teams=teams,
        providers=providers,
    )
    try:
        decision = await service.preview(
            TaskRouteRequest(
                task="帮我处理一下",
                workspace=tmp_path,
                lease=CapabilityLease(filesystem_read=(".",)),
                budget=teams.get("software").default_budget,
            )
        )
    finally:
        await providers.aclose()
        await database.dispose()

    assert decision.status is TaskRouteStatus.CONFIRMATION_REQUIRED
    assert decision.source is TaskRouteSource.SAFE_FALLBACK
    assert TaskRouteIssueCode.CLASSIFICATION_AMBIGUOUS in {issue.code for issue in decision.issues}
    assert database_path.exists() is False
