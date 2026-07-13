"""Session-scoped repositories returning domain models rather than ORM rows."""

from __future__ import annotations

import json
from datetime import datetime
from typing import cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentcell.errors import (
    ConfigurationError,
    EventPayloadTooLargeError,
    EventPayloadTypeError,
    InvalidEventCursorError,
    RunAlreadyExistsError,
    RunNotFoundError,
    StorageIntegrityError,
    StoredEventDataError,
)
from agentcell.events import (
    DomainEvent,
    EventPayload,
    EventType,
    JsonValue,
    parse_event_payload,
    payload_model_for,
)
from agentcell.kernel.models import Run
from agentcell.storage.tables import RunEventRow, RunRow

MAX_INLINE_EVENT_PAYLOAD_BYTES = 64 * 1_024


class RunRepository:
    """Persist Run projections without making lifecycle decisions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, run: Run) -> Run:
        """Insert a new Run projection in the current transaction."""

        row = RunRow(
            id=run.id,
            conversation_id=run.conversation_id,
            agent_id=run.agent_id,
            parent_run_id=run.parent_run_id,
            status=run.status.value,
            created_at=run.created_at,
            updated_at=run.updated_at,
            next_event_sequence=1,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            message = str(exc.orig).casefold()
            if "unique constraint failed: runs.id" in message:
                raise RunAlreadyExistsError(str(run.id)) from exc
            raise StorageIntegrityError(f"Could not create Run {run.id}") from exc
        return run

    async def get(self, run_id: UUID) -> Run | None:
        """Return a Run domain model or ``None`` when it does not exist."""

        row = await self._session.get(RunRow, run_id)
        return None if row is None else self._to_domain(row)

    async def save(self, run: Run) -> Run:
        """Save an already validated Run projection without changing event sequence state."""

        statement = (
            update(RunRow)
            .where(RunRow.id == run.id)
            .values(
                conversation_id=run.conversation_id,
                agent_id=run.agent_id,
                parent_run_id=run.parent_run_id,
                status=run.status.value,
                created_at=run.created_at,
                updated_at=run.updated_at,
            )
            .returning(RunRow.id)
        )
        try:
            result = await self._session.execute(statement)
        except IntegrityError as exc:
            raise StorageIntegrityError(f"Could not save Run {run.id}") from exc
        if result.scalar_one_or_none() is None:
            raise RunNotFoundError(str(run.id))
        return run

    @staticmethod
    def _to_domain(row: RunRow) -> Run:
        return Run.model_validate(
            {
                "id": row.id,
                "conversation_id": row.conversation_id,
                "agent_id": row.agent_id,
                "parent_run_id": row.parent_run_id,
                "status": row.status,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )


class EventStore:
    """Atomically allocate Run-local sequence numbers and append immutable events."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        max_inline_payload_bytes: int = MAX_INLINE_EVENT_PAYLOAD_BYTES,
    ) -> None:
        self._session = session
        if max_inline_payload_bytes <= 0:
            raise ConfigurationError("max_inline_payload_bytes must be positive")
        self._max_inline_payload_bytes = max_inline_payload_bytes

    async def append(
        self,
        *,
        run_id: UUID,
        event_type: EventType,
        payload: EventPayload,
        occurred_at: datetime | None = None,
    ) -> DomainEvent[EventPayload]:
        """Append one validated event inside the caller's transaction."""

        expected_payload = payload_model_for(event_type)
        if not isinstance(payload, expected_payload):
            raise EventPayloadTypeError(
                event_type.value,
                expected_payload.__name__,
                type(payload).__name__,
            )

        safe_payload = payload.safe_dump()
        payload_bytes = len(
            json.dumps(
                safe_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if payload_bytes > self._max_inline_payload_bytes:
            raise EventPayloadTooLargeError(
                payload_bytes,
                self._max_inline_payload_bytes,
            )

        draft_values: dict[str, object] = {
            "run_id": run_id,
            "sequence": 1,
            "event_type": event_type,
            "payload": payload,
        }
        if occurred_at is not None:
            draft_values["occurred_at"] = occurred_at
        draft = DomainEvent[EventPayload].model_validate(draft_values)

        sequence_result = await self._session.execute(
            update(RunRow)
            .where(RunRow.id == run_id)
            .values(next_event_sequence=RunRow.next_event_sequence + 1)
            .returning(RunRow.next_event_sequence)
        )
        next_sequence = sequence_result.scalar_one_or_none()
        if next_sequence is None:
            raise RunNotFoundError(str(run_id))

        event = draft.model_copy(update={"sequence": next_sequence - 1})
        row = RunEventRow(
            id=event.event_id,
            run_id=event.run_id,
            sequence=event.sequence,
            event_type=event.event_type.value,
            payload_version=event.payload.version,
            payload=safe_payload,
            occurred_at=event.occurred_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise StorageIntegrityError(
                f"Could not append event for Run {run_id} at sequence {event.sequence}"
            ) from exc
        return event

    async def list_for_run(
        self,
        run_id: UUID,
        *,
        after_sequence: int = 0,
    ) -> list[DomainEvent[EventPayload]]:
        """Return events ordered by sequence after an exclusive cursor."""

        cursor = self._validate_cursor(after_sequence)
        await self._ensure_run_exists(run_id)

        rows = (
            await self._session.scalars(
                select(RunEventRow)
                .where(
                    RunEventRow.run_id == run_id,
                    RunEventRow.sequence > cursor,
                )
                .order_by(RunEventRow.sequence)
            )
        ).all()
        return [self._to_domain(row) for row in rows]

    async def get_latest_sequence(self, run_id: UUID) -> int:
        """Return the latest committed event sequence, or zero for a Run with no events."""

        next_sequence = await self._session.scalar(
            select(RunRow.next_event_sequence).where(RunRow.id == run_id)
        )
        if next_sequence is None:
            raise RunNotFoundError(str(run_id))
        return next_sequence - 1

    async def count_for_run(self, run_id: UUID) -> int:
        """Return the number of persisted events for integrity checks and diagnostics."""

        await self._ensure_run_exists(run_id)
        count = await self._session.scalar(
            select(func.count()).select_from(RunEventRow).where(RunEventRow.run_id == run_id)
        )
        return int(count or 0)

    async def _ensure_run_exists(self, run_id: UUID) -> None:
        exists = await self._session.scalar(select(RunRow.id).where(RunRow.id == run_id))
        if exists is None:
            raise RunNotFoundError(str(run_id))

    @staticmethod
    def _validate_cursor(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidEventCursorError(value)
        return value

    @staticmethod
    def _to_domain(row: RunEventRow) -> DomainEvent[EventPayload]:
        try:
            event_type = EventType(row.event_type)
            payload_data = cast(dict[str, JsonValue], row.payload)
            payload = parse_event_payload(event_type, payload_data)
            if payload.version != row.payload_version:
                raise ValueError("payload version column does not match payload data")
            return DomainEvent[EventPayload](
                event_id=row.id,
                run_id=row.run_id,
                sequence=row.sequence,
                event_type=event_type,
                occurred_at=row.occurred_at,
                payload=payload,
            )
        except (ValidationError, ValueError) as exc:
            raise StoredEventDataError(str(row.id)) from exc
