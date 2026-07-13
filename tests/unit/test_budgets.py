from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agentcell.budgets import Budget, BudgetTracker, Usage
from agentcell.errors import BudgetExceededError, InvalidBudgetUsageError


@dataclass
class FakeClock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_budget(**overrides: object) -> Budget:
    values: dict[str, object] = {
        "max_requests": 3,
        "max_input_tokens": 100,
        "max_output_tokens": 50,
        "max_total_tokens": 120,
        "max_tool_calls": 4,
        "max_duration_seconds": 30,
        "max_cost": Decimal("2.50"),
        "max_children": 2,
        "max_depth": 2,
    }
    values.update(overrides)
    return Budget.model_validate(values)


def test_budget_rejects_negative_values_unknown_fields_and_boolean_counts() -> None:
    with pytest.raises(ValidationError):
        make_budget(max_requests=-1)

    with pytest.raises(ValidationError):
        make_budget(max_requests=True)

    with pytest.raises(ValidationError):
        make_budget(unknown_limit=1)


def test_tracker_accounts_for_requests_usage_tools_children_and_duration() -> None:
    clock = FakeClock()
    tracker = BudgetTracker(make_budget(), clock=clock)

    tracker.reserve_model_request()
    tracker.record_model_usage(
        input_tokens=25,
        output_tokens=10,
        cost=Decimal("0.75"),
    )
    tracker.reserve_tool_call()
    tracker.reserve_child(depth=2)
    clock.advance(4.5)

    snapshot = tracker.snapshot()

    assert snapshot.used.requests == 1
    assert snapshot.used.total_tokens == 35
    assert snapshot.used.tool_calls == 1
    assert snapshot.used.children == 1
    assert snapshot.used.max_depth_reached == 2
    assert snapshot.used.duration_seconds == 4.5
    assert snapshot.used.cost == Decimal("0.75")
    assert snapshot.remaining.requests == 2
    assert snapshot.remaining.total_tokens == 85
    assert snapshot.remaining.duration_seconds == 25.5
    assert snapshot.remaining.cost == Decimal("1.75")
    assert snapshot.remaining.depth == 0


def test_reservation_failure_does_not_consume_the_rejected_request() -> None:
    tracker = BudgetTracker(make_budget(max_requests=1), clock=FakeClock())
    tracker.reserve_model_request()

    with pytest.raises(BudgetExceededError) as captured:
        tracker.reserve_model_request()

    assert captured.value.resource == "requests"
    assert captured.value.limit == 1
    assert captured.value.attempted == 2
    assert tracker.usage.requests == 1


def test_actual_model_usage_is_retained_when_it_exceeds_the_limit() -> None:
    tracker = BudgetTracker(make_budget(max_output_tokens=5), clock=FakeClock())
    tracker.reserve_model_request()

    with pytest.raises(BudgetExceededError) as captured:
        tracker.record_model_usage(
            input_tokens=1,
            output_tokens=6,
            cost=Decimal("0.10"),
        )

    assert captured.value.resource == "output_tokens"
    assert tracker.usage.output_tokens == 6
    assert tracker.usage.cost == Decimal("0.10")
    assert tracker.remaining.output_tokens == 0


def test_total_token_limit_is_enforced_independently() -> None:
    tracker = BudgetTracker(
        make_budget(max_input_tokens=100, max_output_tokens=100, max_total_tokens=10),
        clock=FakeClock(),
    )
    tracker.reserve_model_request()

    with pytest.raises(BudgetExceededError) as captured:
        tracker.record_model_usage(
            input_tokens=6,
            output_tokens=5,
            cost=Decimal("0"),
        )

    assert captured.value.resource == "total_tokens"
    assert tracker.usage.total_tokens == 11


def test_duration_equal_to_limit_is_allowed_but_greater_is_rejected() -> None:
    clock = FakeClock()
    tracker = BudgetTracker(make_budget(max_duration_seconds=10), clock=clock)

    clock.advance(10)
    tracker.ensure_within_budget()

    clock.advance(0.01)
    with pytest.raises(BudgetExceededError) as captured:
        tracker.ensure_within_budget()

    assert captured.value.resource == "duration_seconds"


def test_restored_usage_is_validated_and_continues_accumulating_duration() -> None:
    clock = FakeClock(100.0)
    initial = Usage(requests=1, duration_seconds=5.0)
    tracker = BudgetTracker(make_budget(), initial_usage=initial, clock=clock)

    clock.advance(2.0)
    assert tracker.usage.duration_seconds == 7.0

    with pytest.raises(BudgetExceededError):
        BudgetTracker(
            make_budget(max_requests=0),
            initial_usage=initial,
            clock=clock,
        )


def test_child_depth_and_usage_deltas_are_validated() -> None:
    tracker = BudgetTracker(make_budget(max_depth=1), clock=FakeClock())

    with pytest.raises(InvalidBudgetUsageError):
        tracker.reserve_child(depth=0)

    with pytest.raises(BudgetExceededError) as captured:
        tracker.reserve_child(depth=2)

    assert captured.value.resource == "depth"
    assert tracker.usage.children == 0

    with pytest.raises(InvalidBudgetUsageError):
        tracker.record_model_usage(
            input_tokens=-1,
            output_tokens=0,
            cost=Decimal("0"),
        )


def test_child_budget_must_fit_capacity_remaining_after_reservation() -> None:
    tracker = BudgetTracker(make_budget(), clock=FakeClock())
    child_budget = make_budget(
        max_children=1,
        max_depth=1,
    )

    usage = tracker.reserve_child(depth=1, child_budget=child_budget)

    assert usage.children == 1
    assert tracker.remaining.children == 1


def test_rejected_child_budget_does_not_consume_a_child_slot() -> None:
    tracker = BudgetTracker(make_budget(), clock=FakeClock())
    child_budget = make_budget(max_requests=4, max_children=1, max_depth=1)

    with pytest.raises(BudgetExceededError) as captured:
        tracker.reserve_child(depth=1, child_budget=child_budget)

    assert captured.value.resource == "child.requests"
    assert captured.value.limit == 3
    assert captured.value.attempted == 4
    assert tracker.usage.children == 0


def test_limited_parent_rejects_child_without_a_cost_limit() -> None:
    tracker = BudgetTracker(make_budget(), clock=FakeClock())
    child_budget = make_budget(max_cost=None, max_children=1, max_depth=1)

    with pytest.raises(BudgetExceededError) as captured:
        tracker.reserve_child(depth=1, child_budget=child_budget)

    assert captured.value.resource == "child.cost"
    assert captured.value.attempted is None
    assert tracker.usage.children == 0


def test_snapshot_rejects_naive_capture_time() -> None:
    tracker = BudgetTracker(make_budget(), clock=FakeClock())

    with pytest.raises(ValidationError):
        tracker.snapshot(captured_at=datetime(2026, 7, 10, 12, 0))


def test_snapshot_serializes_decimal_cost_and_normalized_utc_time() -> None:
    tracker = BudgetTracker(make_budget(), clock=FakeClock())
    tracker.reserve_model_request()
    tracker.record_model_usage(
        input_tokens=1,
        output_tokens=1,
        cost=Decimal("0.10"),
    )
    captured_at = datetime(2026, 7, 10, 20, 30, tzinfo=timezone(timedelta(hours=8)))

    serialized = tracker.snapshot(captured_at=captured_at).model_dump(mode="json")

    assert serialized["used"]["cost"] == "0.10"
    assert serialized["remaining"]["cost"] == "2.40"
    assert serialized["captured_at"] == "2026-07-10T12:30:00Z"
