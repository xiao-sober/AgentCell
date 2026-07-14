"""PydanticAI usage failures retain AgentCell's concrete budget dimension."""

from __future__ import annotations

from pathlib import Path

from pydantic_ai.exceptions import UsageLimitExceeded

from agentcell.budgets import Budget, Usage
from agentcell.kernel.run_service import RunRequest, classify_usage_limit


def _budget() -> Budget:
    return Budget(
        max_requests=10,
        max_input_tokens=100_000,
        max_output_tokens=20_000,
        max_total_tokens=120_000,
        max_tool_calls=20,
        max_duration_seconds=300,
        max_children=0,
        max_depth=0,
    )


def test_default_run_budget_supports_larger_bounded_repository_analysis() -> None:
    budget = RunRequest(prompt="inspect", workspace=Path(".")).budget

    assert budget.max_input_tokens == 200_000
    assert budget.max_output_tokens == 40_000
    assert budget.max_total_tokens == 240_000


def test_tool_call_limit_maps_remaining_usage_to_absolute_budget() -> None:
    error = UsageLimitExceeded(
        "The next tool call(s) would exceed the tool_calls_limit of 2 (tool_calls=5)."
    )

    classified = classify_usage_limit(
        error,
        budget=_budget(),
        usage_at_start=Usage(tool_calls=18),
    )

    assert classified.resource == "tool_calls"
    assert classified.limit == 20
    assert classified.attempted == 23
    assert str(classified) == "Budget for 'tool_calls' exceeded: limit=20, attempted=23"


def test_request_limit_without_attempted_value_reports_next_request() -> None:
    error = UsageLimitExceeded("The next request would exceed the request_limit of 3")

    classified = classify_usage_limit(
        error,
        budget=_budget(),
        usage_at_start=Usage(requests=7),
    )

    assert classified.resource == "requests"
    assert classified.limit == 10
    assert classified.attempted == 11
