"""Run-bound event sink that persists each emitted fact through EventStore."""

from __future__ import annotations

from uuid import UUID

from agentcell.events import DomainEvent, EventPayload, EventType
from agentcell.storage import Database, EventStore


class RunEventRecorder:
    """Bind generic event producers to one Run and transactional EventStore appends."""

    def __init__(self, database: Database, run_id: UUID) -> None:
        self._database = database
        self.run_id = run_id

    async def emit(self, event_type: EventType, payload: EventPayload) -> None:
        async with self._database.transaction() as session:
            await EventStore(session).append(
                run_id=self.run_id,
                event_type=event_type,
                payload=payload,
            )

    async def list(self) -> list[DomainEvent[EventPayload]]:
        async with self._database.session() as session:
            return await EventStore(session).list_for_run(self.run_id)
