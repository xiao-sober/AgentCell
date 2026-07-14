"""AG-UI mapping keeps stable tool identities and resumable composite cursors."""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from ag_ui.core import BaseEvent

from agentcell.api.agui import AgUiEventMapper, AgUiMappingState
from agentcell.api.sse import StreamCursor
from agentcell.errors import InvalidEventCursorError
from agentcell.events import DomainEvent, EventType, GenericEventPayload, JsonValue


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
