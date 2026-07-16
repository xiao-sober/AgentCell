"""Restart-safe AG-UI Server-Sent Events over the append-only event store."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from time import monotonic
from uuid import UUID

from ag_ui.encoder import EventEncoder
from fastapi import Request

from agentcell.api.agui import AgUiEventMapper, AgUiMappingState
from agentcell.application import AgentCellApplication
from agentcell.errors import InvalidEventCursorError, RunNotFoundError
from agentcell.events import EventType

_TERMINAL_EVENTS = {
    EventType.RUN_COMPLETED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELLED,
}


@dataclass(frozen=True, slots=True)
class StreamCursor:
    sequence: int = 0
    offset: int = -1

    @classmethod
    def parse(cls, value: str | None, *, after_sequence: int | None = None) -> StreamCursor:
        if value is None:
            return cls(sequence=after_sequence or 0, offset=10_000 if after_sequence else -1)
        try:
            sequence_text, separator, offset_text = value.partition(".")
            sequence = int(sequence_text)
            offset = int(offset_text) if separator else 10_000
            if sequence < 0 or offset < 0:
                raise ValueError
        except ValueError as error:
            raise InvalidEventCursorError(value) from error
        return cls(sequence=sequence, offset=offset)


async def stream_run_events(
    application: AgentCellApplication,
    request: Request,
    run_id: UUID,
    cursor: StreamCursor,
    *,
    poll_interval: float = 0.1,
    heartbeat_seconds: float = 15,
) -> AsyncIterator[str]:
    """Replay committed history, then tail new events until a terminal event."""

    if await application.get_run(run_id) is None:
        raise RunNotFoundError(str(run_id))
    mapper = AgUiEventMapper()
    state = AgUiMappingState()
    encoder = EventEncoder(accept="text/event-stream")
    last_sequence = 0
    heartbeat_at = monotonic()

    while True:
        events = await application.events(run_id, after_sequence=last_sequence)
        terminal_seen = False
        for domain_event in events:
            last_sequence = domain_event.sequence
            mapped = mapper.map(domain_event, state)
            for offset, agui_event in enumerate(mapped):
                if domain_event.sequence < cursor.sequence:
                    continue
                if domain_event.sequence == cursor.sequence and offset <= cursor.offset:
                    continue
                encoded = encoder.encode(agui_event)
                yield f"id: {domain_event.sequence}.{offset}\n{encoded}"
            terminal_seen = terminal_seen or domain_event.event_type in _TERMINAL_EVENTS
        if terminal_seen:
            return
        if await request.is_disconnected():
            return
        now = monotonic()
        if now - heartbeat_at >= heartbeat_seconds:
            yield ": heartbeat\n\n"
            heartbeat_at = now
        await asyncio.sleep(poll_interval)
