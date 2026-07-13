from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from agentcell.errors import (
    EventPayloadTooLargeError,
    EventPayloadTypeError,
    InvalidEventCursorError,
    RunNotFoundError,
)
from agentcell.events import (
    REDACTED_VALUE,
    ErrorPayload,
    EventType,
    GenericEventPayload,
    TextDeltaPayload,
)
from agentcell.kernel import Run
from agentcell.storage import Database, EventStore, RunRepository


async def _create_run(database: Database) -> Run:
    run = Run(conversation_id=uuid4(), agent_id="coordinator")
    async with database.transaction() as session:
        await RunRepository(session).create(run)
    return run


async def _append_generic_event(
    database: Database,
    run_id: UUID,
    index: int,
) -> int:
    async with database.transaction() as session:
        event = await EventStore(session).append(
            run_id=run_id,
            event_type=EventType.RUN_STARTED,
            payload=GenericEventPayload(data={"index": index}),
        )
    return event.sequence


@pytest.mark.asyncio
async def test_event_store_round_trips_typed_payloads_and_cursor_queries(
    database: Database,
) -> None:
    run = await _create_run(database)
    async with database.transaction() as session:
        store = EventStore(session)
        first = await store.append(
            run_id=run.id,
            event_type=EventType.RUN_STARTED,
            payload=GenericEventPayload(
                data={"agent_id": run.agent_id, "api_key": "must-not-persist"}
            ),
        )
        second = await store.append(
            run_id=run.id,
            event_type=EventType.MODEL_TEXT_DELTA,
            payload=TextDeltaPayload(delta="hello"),
            occurred_at=datetime(2026, 7, 10, 12, 30, tzinfo=UTC),
        )
        third = await store.append(
            run_id=run.id,
            event_type=EventType.MODEL_FAILED,
            payload=ErrorPayload(
                code="provider_timeout",
                message="Provider timed out",
                retryable=True,
            ),
        )

    assert [first.sequence, second.sequence, third.sequence] == [1, 2, 3]

    async with database.session() as session:
        store = EventStore(session)
        events = await store.list_for_run(run.id)
        after_first = await store.list_for_run(run.id, after_sequence=1)
        latest = await store.get_latest_sequence(run.id)
        count = await store.count_for_run(run.id)

    assert [event.sequence for event in events] == [1, 2, 3]
    assert isinstance(events[0].payload, GenericEventPayload)
    assert events[0].payload.data["api_key"] == REDACTED_VALUE
    assert isinstance(events[1].payload, TextDeltaPayload)
    assert isinstance(events[2].payload, ErrorPayload)
    assert events[1].occurred_at == datetime(2026, 7, 10, 12, 30, tzinfo=UTC)
    assert [event.sequence for event in after_first] == [2, 3]
    assert latest == 3
    assert count == 3


@pytest.mark.asyncio
async def test_event_store_allocates_contiguous_sequences_across_concurrent_sessions(
    database: Database,
) -> None:
    run = await _create_run(database)

    sequences = await asyncio.gather(
        *(_append_generic_event(database, run.id, index) for index in range(10))
    )

    assert sorted(sequences) == list(range(1, 11))
    async with database.session() as session:
        events = await EventStore(session).list_for_run(run.id)
    assert [event.sequence for event in events] == list(range(1, 11))


@pytest.mark.asyncio
async def test_event_append_and_sequence_allocation_roll_back_together(
    database: Database,
) -> None:
    run = await _create_run(database)

    with pytest.raises(RuntimeError, match="rollback event"):
        async with database.transaction() as session:
            await EventStore(session).append(
                run_id=run.id,
                event_type=EventType.RUN_STARTED,
                payload=GenericEventPayload(),
            )
            raise RuntimeError("rollback event")

    sequence = await _append_generic_event(database, run.id, 1)
    assert sequence == 1


@pytest.mark.asyncio
async def test_event_store_rejects_wrong_payload_invalid_cursor_and_unknown_run(
    database: Database,
) -> None:
    run = await _create_run(database)

    with pytest.raises(EventPayloadTypeError):
        async with database.transaction() as session:
            await EventStore(session).append(
                run_id=run.id,
                event_type=EventType.RUN_STARTED,
                payload=TextDeltaPayload(delta="wrong schema"),
            )

    with pytest.raises(InvalidEventCursorError):
        async with database.session() as session:
            await EventStore(session).list_for_run(run.id, after_sequence=-1)

    with pytest.raises(RunNotFoundError):
        async with database.transaction() as session:
            await EventStore(session).append(
                run_id=uuid4(),
                event_type=EventType.RUN_STARTED,
                payload=GenericEventPayload(),
            )

    assert await _append_generic_event(database, run.id, 1) == 1


@pytest.mark.asyncio
async def test_database_triggers_reject_event_update_and_delete(database: Database) -> None:
    run = await _create_run(database)
    await _append_generic_event(database, run.id, 1)

    with pytest.raises(DatabaseError, match="append-only"):
        async with database.transaction() as session:
            await session.execute(
                text("UPDATE run_events SET event_type = :event_type WHERE run_id = :run_id"),
                {"event_type": EventType.RUN_COMPLETED.value, "run_id": run.id.hex},
            )

    with pytest.raises(DatabaseError, match="append-only"):
        async with database.transaction() as session:
            await session.execute(
                text("DELETE FROM run_events WHERE run_id = :run_id"),
                {"run_id": run.id.hex},
            )

    async with database.session() as session:
        events = await EventStore(session).list_for_run(run.id)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_oversized_inline_payload_is_rejected_without_consuming_sequence(
    database: Database,
) -> None:
    run = await _create_run(database)

    with pytest.raises(EventPayloadTooLargeError):
        async with database.transaction() as session:
            await EventStore(session).append(
                run_id=run.id,
                event_type=EventType.RUN_STARTED,
                payload=GenericEventPayload(data={"output": "x" * 70_000}),
            )

    assert await _append_generic_event(database, run.id, 1) == 1
