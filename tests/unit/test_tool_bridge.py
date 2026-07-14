"""Model-facing tool aliases and final-response reservation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from agentcell.budgets import Budget, BudgetTracker
from agentcell.kernel.deps import RunDeps
from agentcell.kernel.tool_bridge import (
    budget_instructions,
    build_agent_tools,
    reserve_final_model_request,
)
from agentcell.providers.tool_names import portable_tool_name
from agentcell.tools import ToolRegistry, register_delegation_tool, register_workspace_tools


def _tracker(
    max_requests: int,
    *,
    max_input_tokens: int = 1_000,
    max_output_tokens: int = 1_000,
    max_total_tokens: int = 2_000,
    max_tool_calls: int = 10,
) -> BudgetTracker:
    return BudgetTracker(
        Budget(
            max_requests=max_requests,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            max_total_tokens=max_total_tokens,
            max_tool_calls=max_tool_calls,
            max_duration_seconds=60,
            max_children=1,
            max_depth=1,
        )
    )


def _context(tracker: BudgetTracker) -> RunContext[RunDeps]:
    return RunContext(
        deps=cast(
            RunDeps,
            SimpleNamespace(
                budget=tracker,
                has_deferred_tool_results=False,
            ),
        ),
        model=TestModel(),
        usage=RunUsage(),
    )


def test_domain_tool_names_are_exposed_through_portable_aliases() -> None:
    registry = ToolRegistry()
    register_workspace_tools(registry)
    register_delegation_tool(registry)

    tools = build_agent_tools(("workspace.list", "agent.delegate"), registry)

    assert [tool.name for tool in tools] == ["workspace_list", "agent_delegate"]
    assert portable_tool_name("workspace.list") == "workspace_list"


def test_final_attempt_window_hides_tools_but_deferred_recovery_keeps_them() -> None:
    definition = ToolDefinition(name="workspace_list")
    tracker = _tracker(max_requests=10)
    for _ in range(8):
        tracker.reserve_model_request()
    base_deps = SimpleNamespace(
        budget=tracker,
        has_deferred_tool_results=False,
    )
    context = RunContext(
        deps=cast(RunDeps, base_deps),
        model=TestModel(),
        usage=RunUsage(),
    )

    assert reserve_final_model_request(context, definition) is None
    assert "reserved final-answer window" in budget_instructions(context)

    deferred_context = RunContext(
        deps=cast(
            RunDeps,
            SimpleNamespace(
                budget=base_deps.budget,
                has_deferred_tool_results=True,
            ),
        ),
        model=TestModel(),
        usage=RunUsage(),
    )
    assert reserve_final_model_request(deferred_context, definition) is definition


def test_small_budget_keeps_one_exploration_request() -> None:
    definition = ToolDefinition(name="workspace_list")
    tracker = _tracker(max_requests=2)
    context = _context(tracker)

    assert reserve_final_model_request(context, definition) is definition

    tracker.reserve_model_request()

    assert reserve_final_model_request(context, definition) is None


def test_budget_instruction_is_stable_until_final_window() -> None:
    tracker = _tracker(
        max_requests=20,
        max_input_tokens=20_000,
        max_output_tokens=4_000,
        max_total_tokens=24_000,
        max_tool_calls=40,
    )
    context = _context(tracker)
    initial = budget_instructions(context)

    for _ in range(4):
        tracker.reserve_model_request()
        tracker.record_model_usage(input_tokens=100, output_tokens=20)

    assert budget_instructions(context) == initial
    assert "fixed limits of 20 model requests" in initial
    assert "remaining" not in initial


def test_low_remaining_input_tokens_enter_final_answer_window_early() -> None:
    definition = ToolDefinition(name="workspace_list")
    tracker = _tracker(
        max_requests=20,
        max_input_tokens=20_000,
        max_output_tokens=4_000,
        max_total_tokens=24_000,
        max_tool_calls=40,
    )
    for _ in range(3):
        tracker.reserve_model_request()
        tracker.record_model_usage(input_tokens=4_000, output_tokens=100)
    context = _context(tracker)

    assert tracker.remaining.requests == 17
    assert tracker.remaining.tool_calls == 40
    assert reserve_final_model_request(context, definition) is None
    assert "reserved final-answer window" in budget_instructions(context)
