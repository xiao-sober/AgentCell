"""RunModel forwards Provider cache usage into Run-level accounting."""

from __future__ import annotations

from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from agentcell.budgets import Budget, BudgetTracker
from agentcell.events import EventPayload, EventType
from agentcell.kernel.model_runtime import RunModel


class RecordingEventSink:
    def __init__(self) -> None:
        self.events: list[tuple[EventType, EventPayload]] = []

    async def emit(self, event_type: EventType, payload: EventPayload) -> None:
        self.events.append((event_type, payload))


async def test_model_runtime_records_cache_read_and_write_tokens() -> None:
    tracker = BudgetTracker(
        Budget(
            max_requests=10,
            max_input_tokens=1_000,
            max_output_tokens=1_000,
            max_total_tokens=2_000,
            max_tool_calls=10,
            max_duration_seconds=60,
            max_children=0,
            max_depth=0,
        )
    )
    events = RecordingEventSink()
    runtime = RunModel(
        TestModel(),
        provider="test",
        model_name="test-model",
        budget=tracker,
        events=events,
    )

    await runtime._before_request()  # pyright: ignore[reportPrivateUsage]
    await runtime._record_completion(  # pyright: ignore[reportPrivateUsage]
        RequestUsage(
            input_tokens=100,
            output_tokens=20,
            cache_write_tokens=30,
            cache_read_tokens=75,
        )
    )

    assert tracker.usage.input_tokens == 100
    assert tracker.usage.output_tokens == 20
    assert tracker.usage.cache_write_tokens == 30
    assert tracker.usage.cache_read_tokens == 75
    assert [event_type for event_type, _ in events.events] == [
        EventType.BUDGET_UPDATED,
        EventType.MODEL_REQUESTED,
        EventType.MODEL_COMPLETED,
        EventType.BUDGET_UPDATED,
    ]
