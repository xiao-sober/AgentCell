"""Least-authority validation for proposed task routes."""

from __future__ import annotations

from collections.abc import Iterable

from agentcell.agents import AgentRegistry, AgentVisibility, TeamRegistry
from agentcell.budgets import Budget
from agentcell.errors import (
    AgentNotFoundError,
    ProviderConfigurationError,
    TeamNotFoundError,
)
from agentcell.policy import Capability, CapabilityLease
from agentcell.providers import ProviderFactory
from agentcell.routing.models import (
    RoutingPolicy,
    TaskRouteIssue,
    TaskRouteIssueCode,
    TaskRouteMode,
)


def capability_gaps(
    required: Iterable[Capability],
    lease: CapabilityLease,
) -> frozenset[Capability]:
    """Return required coarse capabilities absent from the caller-supplied lease."""

    return frozenset(capability for capability in required if not lease.allows(capability))


def validate_target(
    *,
    mode: TaskRouteMode,
    target_id: str,
    required_capabilities: frozenset[Capability],
    model_ref: str | None,
    budget: Budget,
    agents: AgentRegistry,
    teams: TeamRegistry,
    providers: ProviderFactory,
    policy: RoutingPolicy,
) -> tuple[TaskRouteIssue, ...]:
    """Validate target visibility, configured models, and root execution budget."""

    issues: list[TaskRouteIssue] = []
    if mode is TaskRouteMode.SINGLE_AGENT:
        if target_id not in policy.public_agent_ids:
            return (
                TaskRouteIssue(
                    code=TaskRouteIssueCode.TARGET_NOT_PUBLIC,
                    message=(
                        f"Agent {target_id!r} is not allowed by RoutingPolicy "
                        f"{policy.policy_version}."
                    ),
                ),
            )
        try:
            entry = agents.get_entry(target_id)
        except AgentNotFoundError:
            return (
                TaskRouteIssue(
                    code=TaskRouteIssueCode.TARGET_UNAVAILABLE,
                    message=f"Agent {target_id!r} is not registered.",
                ),
            )
        if entry.visibility is not AgentVisibility.PUBLIC:
            issues.append(
                TaskRouteIssue(
                    code=TaskRouteIssueCode.TARGET_NOT_PUBLIC,
                    message=f"Agent {target_id!r} is internal and cannot be routed publicly.",
                )
            )
        if not required_capabilities.issubset(entry.spec.capabilities):
            issues.append(
                TaskRouteIssue(
                    code=TaskRouteIssueCode.TARGET_UNAVAILABLE,
                    message=f"Agent {target_id!r} cannot satisfy the required capabilities.",
                )
            )
        selected_refs = (model_ref or entry.spec.model_ref,)
        if budget.max_children < 1 or budget.max_depth < 1 or budget.max_requests < 1:
            issues.append(
                TaskRouteIssue(
                    code=TaskRouteIssueCode.BUDGET_INSUFFICIENT,
                    message="Task root budget must allow one child, depth one, and one request.",
                )
            )
    else:
        if target_id not in policy.public_team_ids:
            return (
                TaskRouteIssue(
                    code=TaskRouteIssueCode.TARGET_NOT_PUBLIC,
                    message=(
                        f"Team {target_id!r} is not allowed by RoutingPolicy "
                        f"{policy.policy_version}."
                    ),
                ),
            )
        try:
            team = teams.get(target_id)
        except TeamNotFoundError:
            return (
                TaskRouteIssue(
                    code=TaskRouteIssueCode.TARGET_UNAVAILABLE,
                    message=f"Team {target_id!r} is not registered.",
                ),
            )
        try:
            team.validate_agents(agents)
            team.allocate_stage_budgets(budget)
            team_capabilities = frozenset(
                capability for stage in team.stages for capability in stage.capabilities
            )
            if not required_capabilities.issubset(team_capabilities):
                issues.append(
                    TaskRouteIssue(
                        code=TaskRouteIssueCode.TARGET_UNAVAILABLE,
                        message=f"Team {target_id!r} cannot satisfy the required capabilities.",
                    )
                )
        except AgentNotFoundError:
            issues.append(
                TaskRouteIssue(
                    code=TaskRouteIssueCode.TARGET_UNAVAILABLE,
                    message=f"Team {target_id!r} references an unavailable Agent.",
                )
            )
        except ValueError as error:
            issues.append(
                TaskRouteIssue(
                    code=TaskRouteIssueCode.BUDGET_INSUFFICIENT,
                    message=str(error),
                )
            )
        selected_refs = (
            (model_ref,)
            if model_ref is not None
            else tuple(dict.fromkeys(stage.model_ref for stage in team.stages))
        )

    for selected_ref in selected_refs:
        try:
            providers.model_spec(selected_ref)
        except ProviderConfigurationError:
            issues.append(
                TaskRouteIssue(
                    code=TaskRouteIssueCode.PROVIDER_UNAVAILABLE,
                    message=f"Model reference {selected_ref!r} is not configured.",
                )
            )
    return tuple(issues)
