"""Versioned deterministic Team declarations and least-authority allocation."""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    model_validator,
)

from agentcell.agents.delegation import HandoffStage
from agentcell.agents.registry import AgentRegistry
from agentcell.budgets import FINAL_OUTPUT_ATTEMPTS, Budget
from agentcell.errors import TeamNotFoundError, TeamRegistrationError
from agentcell.policy import Capability, CapabilityLease

_TEST_REPAIR_INTENT = re.compile(
    r"(?:修复|解决|处理).{0,16}(?:测试|用例)|"
    r"(?:测试|用例).{0,16}(?:失败|报错|不通过)|"
    r"\b(?:fix|repair).{0,24}\btests?\b|\bmake\s+(?:the\s+)?tests?\s+pass\b",
    re.IGNORECASE,
)
_ADDITIVE_INTENT = re.compile(
    r"(?:实现|新增|添加|开发|重构).{0,24}(?:功能|特性|模块|接口|页面)|"
    r"\b(?:implement|add|create|build|refactor)\b.{0,32}\b(?:feature|module|api|page)\b",
    re.IGNORECASE,
)


def is_test_repair_task(task: str) -> bool:
    """Conservatively identify tasks whose only implementation goal is repairing tests."""

    return bool(_TEST_REPAIR_INTENT.search(task)) and not _ADDITIVE_INTENT.search(task)


class TeamStageSpec(BaseModel):
    """One deterministic stage with explicit inputs, authority, model, and budget weight."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: HandoffStage
    agent_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    model_ref: str = Field(min_length=1)
    depends_on: tuple[HandoffStage, ...] = ()
    capabilities: frozenset[Capability] = frozenset()
    budget_weight: int = Field(default=1, ge=1, le=100, strict=True)
    min_requests: int = Field(default=1, ge=1, le=100, strict=True)
    request_weight: int = Field(default=1, ge=0, le=100, strict=True)
    tool_weight: int = Field(default=1, ge=0, le=100, strict=True)
    max_tool_calls: int | None = Field(default=None, ge=0, strict=True)
    instructions: str = Field(min_length=1, max_length=2_000)
    output_contract: str = Field(min_length=1, max_length=1_000)

    @field_serializer("capabilities", when_used="json")
    def serialize_capabilities(self, value: frozenset[Capability]) -> list[str]:
        return sorted(item.value for item in value)


class TeamSpec(BaseModel):
    """Immutable product-level Team definition shared by CLI and future routing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, strict=True)
    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    stages: tuple[TeamStageSpec, ...] = Field(min_length=1)
    default_budget: Budget
    review_gate: bool = True

    @model_validator(mode="after")
    def validate_pipeline(self) -> TeamSpec:
        expected = tuple(HandoffStage)
        actual = tuple(item.stage for item in self.stages)
        if actual != expected:
            raise ValueError("Team stages must be coordinator, coder, reviewer, finalizer")
        for index, item in enumerate(self.stages):
            allowed_dependencies = set(actual[:index])
            if len(set(item.depends_on)) != len(item.depends_on):
                raise ValueError(f"Team stage {item.stage.value!r} has duplicate dependencies")
            if not set(item.depends_on).issubset(allowed_dependencies):
                raise ValueError(
                    f"Team stage {item.stage.value!r} depends on a later or unknown stage"
                )
        self._validate_root_budget(self.default_budget)
        return self

    def validate_agents(self, agents: AgentRegistry) -> None:
        """Ensure Team stage declarations stay within registered AgentSpec authority."""

        for stage in self.stages:
            spec = agents.get(stage.agent_id)
            if not stage.capabilities.issubset(spec.capabilities):
                raise ValueError(
                    f"Team stage {stage.stage.value!r} exceeds Agent {spec.id!r} capabilities"
                )
        reviewer = self.stage(HandoffStage.REVIEWER)
        if reviewer.capabilities - {Capability.FILESYSTEM_READ}:
            raise ValueError("software Team reviewer must remain read-only")

    def stage(self, stage: HandoffStage) -> TeamStageSpec:
        return next(item for item in self.stages if item.stage is stage)

    def allocate_stage_budgets(self, root: Budget) -> dict[HandoffStage, Budget]:
        """Partition every root resource deterministically across all direct children."""

        self._validate_root_budget(root)
        general_weights = tuple(item.budget_weight for item in self.stages)
        request_weights = tuple(item.request_weight for item in self.stages)
        request_minimums = tuple(item.min_requests for item in self.stages)
        tool_weights = tuple(item.tool_weight for item in self.stages)
        requests = _allocate_int_with_minimums(
            root.max_requests,
            request_weights,
            request_minimums,
        )
        input_tokens = _allocate_int(root.max_input_tokens, general_weights, minimum=1)
        output_tokens = _allocate_int(root.max_output_tokens, general_weights, minimum=1)
        total_tokens = _allocate_int(root.max_total_tokens, general_weights, minimum=1)
        tool_calls = _allocate_int(root.max_tool_calls, tool_weights, minimum=0)
        tool_calls = tuple(
            value if item.max_tool_calls is None else min(value, item.max_tool_calls)
            for item, value in zip(self.stages, tool_calls, strict=True)
        )
        durations = _allocate_int(root.max_duration_seconds, general_weights, minimum=1)
        costs = _allocate_decimal(root.max_cost, general_weights)
        return {
            item.stage: Budget(
                max_requests=requests[index],
                max_input_tokens=input_tokens[index],
                max_output_tokens=output_tokens[index],
                max_total_tokens=total_tokens[index],
                max_tool_calls=tool_calls[index],
                max_duration_seconds=durations[index],
                max_cost=None if costs is None else costs[index],
                max_children=0,
                max_depth=0,
            )
            for index, item in enumerate(self.stages)
        }

    def allocate_stage_leases(
        self,
        root: CapabilityLease,
    ) -> dict[HandoffStage, CapabilityLease]:
        """Filter the root authority envelope through each stage's declared capabilities."""

        leases: dict[HandoffStage, CapabilityLease] = {}
        for item in self.stages:
            lease = CapabilityLease(
                filesystem_read=(
                    root.filesystem_read if Capability.FILESYSTEM_READ in item.capabilities else ()
                ),
                filesystem_write=(
                    root.filesystem_write
                    if Capability.FILESYSTEM_WRITE in item.capabilities
                    else ()
                ),
                network_domains=(
                    root.network_domains if Capability.NETWORK_REQUEST in item.capabilities else ()
                ),
                commands=(
                    root.commands if Capability.SHELL_EXECUTE in item.capabilities else frozenset()
                ),
            )
            root.ensure_child_subset(lease)
            leases[item.stage] = lease
        return leases

    def _validate_root_budget(self, budget: Budget) -> None:
        stage_count = len(self.stages)
        if budget.max_children < stage_count or budget.max_depth < 1:
            raise ValueError("Team root budget must allow four direct children at depth one")
        minimum_requests = sum(item.min_requests for item in self.stages)
        if budget.max_requests < minimum_requests:
            raise ValueError(
                f"Team root requests budget must be at least {minimum_requests} "
                "to preserve stage exploration and final-output retries"
            )
        minimum_dimensions = {
            "input_tokens": budget.max_input_tokens,
            "output_tokens": budget.max_output_tokens,
            "total_tokens": budget.max_total_tokens,
            "duration_seconds": budget.max_duration_seconds,
        }
        for resource, value in minimum_dimensions.items():
            if value < stage_count:
                raise ValueError(
                    f"Team root {resource} budget must allocate at least one per stage"
                )


class TeamRegistry:
    """Small deterministic registry for public, application-owned Team definitions."""

    def __init__(self, teams: tuple[TeamSpec, ...] = ()) -> None:
        self._items: dict[str, TeamSpec] = {}
        for team in teams:
            self.register(team)

    def register(self, team: TeamSpec) -> None:
        if team.id in self._items:
            raise TeamRegistrationError(f"Team {team.id!r} is already registered")
        self._items[team.id] = team

    def get(self, team_id: str) -> TeamSpec:
        try:
            return self._items[team_id]
        except KeyError as error:
            raise TeamNotFoundError(team_id) from error

    def list(self) -> tuple[TeamSpec, ...]:
        return tuple(self._items[key] for key in sorted(self._items))


def software_team_spec(*, model_ref: str) -> TeamSpec:
    """Return the only stage-9.3 product Team: a deterministic software workflow."""

    read = frozenset({Capability.FILESYSTEM_READ})
    return TeamSpec(
        id="software",
        name="Software Delivery",
        description="Plan, implement, independently review, and summarize one software task.",
        stages=(
            TeamStageSpec(
                stage=HandoffStage.COORDINATOR,
                agent_id="coordinator",
                model_ref=model_ref,
                capabilities=read,
                min_requests=3 + FINAL_OUTPUT_ATTEMPTS,
                request_weight=0,
                tool_weight=2,
                max_tool_calls=3,
                instructions=(
                    "Stay at planning altitude. Inspect only the workspace root, governing "
                    "instructions, and at most one project manifest when needed. Use no more "
                    "than three workspace tool calls, do not enumerate or read individual test "
                    "files, and leave detailed diagnosis to Coder. Then return the bounded plan."
                ),
                output_contract="A bounded implementation plan grounded in workspace evidence.",
            ),
            TeamStageSpec(
                stage=HandoffStage.CODER,
                agent_id="coder",
                model_ref=model_ref,
                depends_on=(HandoffStage.COORDINATOR,),
                capabilities=frozenset(
                    {
                        Capability.FILESYSTEM_READ,
                        Capability.FILESYSTEM_WRITE,
                        Capability.SHELL_EXECUTE,
                    }
                ),
                budget_weight=3,
                min_requests=2 + FINAL_OUTPUT_ATTEMPTS,
                request_weight=2,
                tool_weight=5,
                max_tool_calls=27,
                instructions=(
                    "Treat the Coordinator plan as a starting point and run the explicitly leased "
                    "test command before broad source inspection. If the full requested test suite "
                    "exits successfully and the original task only asks to repair tests, stop "
                    "immediately: do not enumerate or read individual tests or source files, make "
                    "no changes, and report that no repair was required. Otherwise inspect only "
                    "files relevant to observed failures, implement the smallest correct change, "
                    "and never claim a test passed without a persisted tool result."
                ),
                output_contract="Changed files, exact checks run, and remaining risks.",
            ),
            TeamStageSpec(
                stage=HandoffStage.REVIEWER,
                agent_id="reviewer",
                model_ref=model_ref,
                depends_on=(HandoffStage.COORDINATOR, HandoffStage.CODER),
                capabilities=read,
                min_requests=1 + FINAL_OUTPUT_ATTEMPTS,
                request_weight=1,
                tool_weight=2,
                max_tool_calls=6,
                instructions=(
                    "Independently review the persisted plan and Coder result. Inspect only files "
                    "needed to verify material claims, remain read-only, and make a decisive gate "
                    "judgment without proposing new tool calls in the final response. When the "
                    "runtime evidence records a successful full test command and zero file "
                    "changes, use that evidence directly and do not inspect the workspace."
                ),
                output_contract="First line PASS or CHANGES_NEEDED, followed by evidence.",
            ),
            TeamStageSpec(
                stage=HandoffStage.FINALIZER,
                agent_id="finalizer",
                model_ref=model_ref,
                depends_on=(
                    HandoffStage.COORDINATOR,
                    HandoffStage.CODER,
                    HandoffStage.REVIEWER,
                ),
                capabilities=frozenset(),
                min_requests=FINAL_OUTPUT_ATTEMPTS,
                request_weight=0,
                tool_weight=0,
                max_tool_calls=0,
                instructions=(
                    "Do not inspect the workspace or call tools. Summarize only the persisted "
                    "Coordinator, Coder, and Reviewer evidence supplied in this prompt."
                ),
                output_contract="A final result containing only persisted stage evidence.",
            ),
        ),
        default_budget=Budget(
            max_requests=24,
            max_input_tokens=240_000,
            max_output_tokens=48_000,
            max_total_tokens=288_000,
            max_tool_calls=48,
            max_duration_seconds=600,
            max_cost=None,
            max_children=4,
            max_depth=1,
        ),
    )


def _allocate_int(total: int, weights: tuple[int, ...], *, minimum: int) -> tuple[int, ...]:
    required = minimum * len(weights)
    if total < required:
        raise ValueError("Team budget is too small for its stage minimum")
    remaining = total - required
    weight_total = sum(weights)
    if weight_total == 0:
        if remaining == 0:
            return tuple(minimum for _ in weights)
        raise ValueError("Team budget weights cannot all be zero for a non-zero resource")
    raw = tuple(remaining * weight for weight in weights)
    allocations = [minimum + value // weight_total for value in raw]
    leftover = total - sum(allocations)
    order = sorted(
        range(len(weights)),
        key=lambda index: (-(raw[index] % weight_total), index),
    )
    for index in order[:leftover]:
        allocations[index] += 1
    return tuple(allocations)


def _allocate_int_with_minimums(
    total: int,
    weights: tuple[int, ...],
    minimums: tuple[int, ...],
) -> tuple[int, ...]:
    if len(weights) != len(minimums):
        raise ValueError("Team budget weights and minimums must have equal length")
    required = sum(minimums)
    if total < required:
        raise ValueError("Team budget is too small for its stage minimums")
    remaining = total - required
    weight_total = sum(weights)
    if remaining == 0:
        return minimums
    if weight_total == 0:
        raise ValueError("Team request weights cannot all be zero with remaining budget")
    raw = tuple(remaining * weight for weight in weights)
    allocations = [
        minimum + value // weight_total for minimum, value in zip(minimums, raw, strict=True)
    ]
    leftover = total - sum(allocations)
    order = sorted(
        range(len(weights)),
        key=lambda index: (-(raw[index] % weight_total), index),
    )
    for index in order[:leftover]:
        allocations[index] += 1
    return tuple(allocations)


def _allocate_decimal(
    total: Decimal | None,
    weights: tuple[int, ...],
) -> tuple[Decimal, ...] | None:
    if total is None:
        return None
    weight_total = Decimal(sum(weights))
    allocations = [total * Decimal(weight) / weight_total for weight in weights]
    allocations[-1] = total - sum(allocations[:-1], Decimal("0"))
    return tuple(allocations)
