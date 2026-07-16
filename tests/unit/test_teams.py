"""Stage 9.3 Team declarations partition authority and resources deterministically."""

from __future__ import annotations

import pytest

from agentcell.agents import (
    AgentRegistry,
    HandoffStage,
    TeamRegistry,
    coder_spec,
    coordinator_spec,
    finalizer_spec,
    is_test_repair_task,
    reviewer_spec,
    software_team_spec,
)
from agentcell.cli.profile import CliTeamProfile, CommandProfile
from agentcell.errors import TeamNotFoundError, TeamRegistrationError
from agentcell.policy import CapabilityLease, PermissionMode


def _agents() -> AgentRegistry:
    return AgentRegistry(
        (
            coordinator_spec(model_ref="fake", collaborative=False),
            coder_spec(model_ref="fake"),
            reviewer_spec(model_ref="fake"),
            finalizer_spec(model_ref="fake"),
        )
    )


def test_software_team_has_versioned_fixed_pipeline() -> None:
    team = software_team_spec(model_ref="fake")

    assert team.schema_version == 1
    assert tuple(item.stage for item in team.stages) == tuple(HandoffStage)
    assert team.stage(HandoffStage.REVIEWER).agent_id == "reviewer"
    assert team.default_budget.max_requests == 24
    assert team.default_budget.max_tool_calls == 48
    assert finalizer_spec(model_ref="fake").tools == ()
    team.validate_agents(_agents())


def test_team_allocations_are_bounded_and_reviewer_is_read_only() -> None:
    team = software_team_spec(model_ref="fake")
    root = CapabilityLease(
        filesystem_read=(".",),
        filesystem_write=("src",),
        commands=frozenset({"pytest"}),
    )

    budgets = team.allocate_stage_budgets(team.default_budget)
    leases = team.allocate_stage_leases(root)

    for field in (
        "max_requests",
        "max_input_tokens",
        "max_output_tokens",
        "max_total_tokens",
        "max_duration_seconds",
    ):
        assert sum(getattr(item, field) for item in budgets.values()) == getattr(
            team.default_budget, field
        )
    assert (
        sum(item.max_tool_calls for item in budgets.values()) <= team.default_budget.max_tool_calls
    )
    assert all(item.max_children == 0 and item.max_depth == 0 for item in budgets.values())
    assert leases[HandoffStage.CODER].filesystem_write == ("src",)
    assert leases[HandoffStage.CODER].commands == frozenset({"pytest"})
    assert leases[HandoffStage.REVIEWER].filesystem_read == (".",)
    assert leases[HandoffStage.REVIEWER].filesystem_write == ()
    assert leases[HandoffStage.REVIEWER].commands == frozenset()
    assert budgets[HandoffStage.COORDINATOR].max_requests == 6
    assert budgets[HandoffStage.COORDINATOR].max_tool_calls == 3
    assert budgets[HandoffStage.CODER].max_requests == 9
    assert budgets[HandoffStage.CODER].max_tool_calls == 27
    assert budgets[HandoffStage.REVIEWER].max_requests == 6
    assert budgets[HandoffStage.REVIEWER].max_tool_calls == 6
    assert budgets[HandoffStage.FINALIZER].max_requests == 3
    assert budgets[HandoffStage.FINALIZER].max_tool_calls == 0
    assert leases[HandoffStage.FINALIZER] == CapabilityLease()


def test_cli_team_profile_applies_root_overrides_before_partitioning() -> None:
    team = software_team_spec(model_ref="fake")
    profile = CliTeamProfile.resolve(
        team,
        _agents(),
        approval_mode=PermissionMode.AUTO,
        permission_mode=None,
        write_scopes=["src"],
        legacy_write_scopes=None,
        commands=None,
        legacy_commands=None,
        command_profiles=[CommandProfile.PYTEST],
        network_domains=None,
        max_requests=18,
        max_tool_calls=12,
        max_input_tokens=80_000,
        max_total_tokens=100_000,
    )

    assert profile.team_id == "software"
    assert profile.approval_mode is PermissionMode.AUTO
    assert profile.budget.max_requests == 18
    assert sum(item.max_requests for item in profile.stage_budgets.values()) == 18
    assert profile.stage_model_refs[HandoffStage.FINALIZER] == "fake"
    assert (
        "no more than three workspace tool calls"
        in profile.stage_instructions[HandoffStage.COORDINATOR]
    )
    assert (
        "full requested test suite exits successfully"
        in profile.stage_instructions[HandoffStage.CODER]
    )
    assert profile.stage_output_contracts[HandoffStage.REVIEWER].startswith("First line PASS")
    assert profile.stage_leases[HandoffStage.REVIEWER].filesystem_write == ()
    assert profile.stage_leases[HandoffStage.FINALIZER] == CapabilityLease()


def test_team_rejects_request_budget_that_cannot_preserve_final_output_retries() -> None:
    team = software_team_spec(model_ref="fake")
    budget = team.default_budget.model_copy(update={"max_requests": 17})

    with pytest.raises(ValueError, match="must be at least 18"):
        team.allocate_stage_budgets(budget)


def test_team_registry_rejects_unknown_team() -> None:
    registry = TeamRegistry((software_team_spec(model_ref="fake"),))

    with pytest.raises(TeamNotFoundError, match="missing"):
        registry.get("missing")

    with pytest.raises(TeamRegistrationError, match="already registered"):
        registry.register(software_team_spec(model_ref="fake"))


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ("修复测试并独立审查", True),
        ("Fix the failing tests and review the result", True),
        ("实现新功能并修复测试", False),
        ("Implement a feature and fix its tests", False),
        ("分析测试结构", False),
    ],
)
def test_test_repair_intent_is_conservative(task: str, expected: bool) -> None:
    assert is_test_repair_task(task) is expected
