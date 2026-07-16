"""AG-UI mapping keeps stable tool identities and resumable composite cursors."""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from ag_ui.core import BaseEvent

from agentcell.api.agui import AgUiEventMapper, AgUiMappingState
from agentcell.api.sse import StreamCursor
from agentcell.errors import InvalidEventCursorError
from agentcell.events import (
    DomainEvent,
    ErrorPayload,
    EventPayload,
    EventType,
    GenericEventPayload,
    JsonValue,
)
from agentcell.events.models import ModelRequestedPayload, TextDeltaPayload


def test_tool_events_keep_provider_call_identity_and_bounded_result() -> None:
    run_id = uuid4()
    mapper = AgUiEventMapper()
    state = AgUiMappingState(last_message_id="assistant-1")
    payloads: tuple[tuple[EventType, dict[str, JsonValue]], ...] = (
        (
            EventType.TOOL_PROPOSED,
            {
                "call_id": "internal-1",
                "provider_call_id": "provider-1",
                "tool_name": "workspace.read",
                "arguments": {"path": "README.md"},
            },
        ),
        (
            EventType.TOOL_STARTED,
            {
                "call_id": "internal-1",
                "provider_call_id": "provider-1",
                "tool_name": "workspace.read",
            },
        ),
        (
            EventType.TOOL_COMPLETED,
            {
                "call_id": "internal-1",
                "provider_call_id": "provider-1",
                "tool_name": "workspace.read",
                "output": "contents",
            },
        ),
    )

    mapped: list[BaseEvent] = []
    for sequence, (event_type, data) in enumerate(payloads, start=1):
        mapped.extend(
            mapper.map(
                DomainEvent(
                    run_id=run_id,
                    sequence=sequence,
                    event_type=event_type,
                    payload=GenericEventPayload(data=data),
                ),
                state,
            )
        )

    serialized = [
        cast(dict[str, JsonValue], event.model_dump(mode="json", by_alias=True)) for event in mapped
    ]
    assert [
        item.get("toolCallId") for item in serialized if item.get("toolCallId") is not None
    ] == [
        "provider-1",
        "provider-1",
        "provider-1",
        "provider-1",
    ]
    assert "contents" in cast(str, serialized[-1]["content"])


def test_stream_cursor_accepts_composite_ids_and_rejects_invalid_values() -> None:
    assert StreamCursor.parse("12.1") == StreamCursor(sequence=12, offset=1)
    assert StreamCursor.parse(None, after_sequence=12) == StreamCursor(
        sequence=12,
        offset=10_000,
    )
    with pytest.raises(InvalidEventCursorError):
        StreamCursor.parse("not-a-cursor")


def test_agui_uses_safe_display_text_and_redacted_tool_payloads() -> None:
    run_id = uuid4()
    mapper = AgUiEventMapper()
    state = AgUiMappingState(thread_id="thread")
    events: tuple[DomainEvent[EventPayload], ...] = (
        cast(
            DomainEvent[EventPayload],
            DomainEvent(
                run_id=run_id,
                sequence=1,
                event_type=EventType.MODEL_REQUESTED,
                payload=ModelRequestedPayload(provider="fake", model="fake", request_index=1),
            ),
        ),
        cast(
            DomainEvent[EventPayload],
            DomainEvent(
                run_id=run_id,
                sequence=2,
                event_type=EventType.MODEL_TEXT_DELTA,
                payload=TextDeltaPayload(delta="answer api_key=topsecret"),
            ),
        ),
        cast(
            DomainEvent[EventPayload],
            DomainEvent(
                run_id=run_id,
                sequence=3,
                event_type=EventType.TOOL_COMPLETED,
                payload=GenericEventPayload(
                    data={
                        "call_id": "call-1",
                        "tool_name": "workspace.read",
                        "output": "password=tool-secret",
                        "reasoning_content": "private reasoning",
                    }
                ),
            ),
        ),
        cast(
            DomainEvent[EventPayload],
            DomainEvent(
                run_id=run_id,
                sequence=4,
                event_type=EventType.RUN_FAILED,
                payload=ErrorPayload(
                    code="provider_error",
                    message="request failed api_key=error-secret",
                ),
            ),
        ),
    )

    mapped = [item for event in events for item in mapper.map(event, state)]
    serialized = "\n".join(item.model_dump_json(by_alias=True) for item in mapped)
    assert "topsecret" not in serialized
    assert "tool-secret" not in serialized
    assert "error-secret" not in serialized
    assert "private reasoning" not in serialized
    assert "reasoning_content" in serialized
    assert "[REDACTED]" in serialized
