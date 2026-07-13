"""Deterministic event replay and checkpoint-backed Run branching."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentcell.errors import InvalidEventCursorError, ReplayError, RunNotFoundError
from agentcell.events import (
    DomainEvent,
    EventPayload,
    EventType,
    GenericEventPayload,
    RunStartedPayload,
    RunStatusChangedPayload,
)
from agentcell.kernel.checkpoint import Checkpoint, CheckpointKind
from agentcell.kernel.lifecycle import RunStatus
from agentcell.kernel.models import Run
from agentcell.storage import CheckpointRepository, Database, EventStore, RunRepository


class ReplayState(BaseModel):
    """Minimal state deterministically projected from one Run event prefix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    through_sequence: int = Field(ge=1, strict=True)
    status: RunStatus
    events_applied: int = Field(ge=1, strict=True)
    completed_provider_calls: frozenset[str] = frozenset()


class ReplayService:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def replay(self, run_id: UUID, *, through_sequence: int | None = None) -> ReplayState:
        async with self._database.session() as session:
            store = EventStore(session)
            events = await store.list_for_run(run_id)
        if not events:
            raise ReplayError("Run has no events")
        latest = events[-1].sequence
        selected_sequence = latest if through_sequence is None else through_sequence
        if selected_sequence < 1 or selected_sequence > latest:
            raise InvalidEventCursorError(selected_sequence)
        selected = [event for event in events if event.sequence <= selected_sequence]
        self._ensure_contiguous(selected)
        status = RunStatus.CREATED
        calls: dict[str, str] = {}
        completed: set[str] = set()
        for event in selected:
            if event.event_type is EventType.RUN_STATUS_CHANGED:
                status = RunStatus(event.payload.safe_dump()["status"])
            elif event.event_type is EventType.TOOL_PROPOSED:
                data = event.payload.safe_dump().get("data")
                if isinstance(data, dict):
                    call_id = data.get("call_id")
                    provider_call_id = data.get("provider_call_id")
                    if isinstance(call_id, str) and isinstance(provider_call_id, str):
                        calls[call_id] = provider_call_id
            elif event.event_type is EventType.TOOL_COMPLETED:
                data = event.payload.safe_dump().get("data")
                if isinstance(data, dict):
                    call_id = data.get("call_id")
                    if isinstance(call_id, str) and call_id in calls:
                        completed.add(calls[call_id])
        return ReplayState(
            run_id=run_id,
            through_sequence=selected_sequence,
            status=status,
            events_applied=len(selected),
            completed_provider_calls=frozenset(completed),
        )

    async def branch(self, run_id: UUID, *, from_sequence: int) -> Run:
        state = await self.replay(run_id, through_sequence=from_sequence)
        async with self._database.session() as session:
            source = await RunRepository(session).get(run_id)
            if source is None:
                raise RunNotFoundError(str(run_id))
            source_checkpoint = await CheckpointRepository(session).latest(
                run_id,
                through_sequence=from_sequence,
            )
        child = Run(
            conversation_id=source.conversation_id,
            agent_id=source.agent_id,
            parent_run_id=source.id,
        )
        running = child.transition_to(RunStatus.RUNNING)
        paused = running.transition_to(RunStatus.PAUSED)
        async with self._database.transaction() as session:
            runs = RunRepository(session)
            store = EventStore(session)
            await runs.create(child)
            await store.append(
                run_id=child.id,
                event_type=EventType.RUN_STARTED,
                payload=RunStartedPayload(
                    conversation_id=child.conversation_id,
                    agent_id=child.agent_id,
                ),
            )
            await runs.save(running)
            await store.append(
                run_id=child.id,
                event_type=EventType.RUN_STATUS_CHANGED,
                payload=RunStatusChangedPayload(
                    previous_status=RunStatus.CREATED,
                    status=RunStatus.RUNNING,
                ),
            )
            checkpoint_event = await store.append(
                run_id=child.id,
                event_type=EventType.CHECKPOINT_CREATED,
                payload=GenericEventPayload(
                    data={
                        "reason": "branch",
                        "source_run_id": str(run_id),
                        "source_sequence": from_sequence,
                        "source_status": state.status.value,
                    }
                ),
            )
            checkpoint = Checkpoint(
                run_id=child.id,
                user_id=source_checkpoint.user_id,
                event_sequence=checkpoint_event.sequence,
                kind=CheckpointKind.BRANCH,
                agent_id=source_checkpoint.agent_id,
                prompt=source_checkpoint.prompt,
                workspace=source_checkpoint.workspace,
                lease=source_checkpoint.lease,
                budget=source_checkpoint.budget,
                messages=source_checkpoint.messages,
                pending_approval_ids=source_checkpoint.pending_approval_ids,
                temporary_approved_tools=source_checkpoint.temporary_approved_tools,
                artifact_ids=source_checkpoint.artifact_ids,
                run_status=RunStatus.PAUSED,
                parent_run_id=source.id,
                source_run_id=source.id,
                source_sequence=from_sequence,
            )
            await CheckpointRepository(session).create(checkpoint)
            await runs.save(paused)
            await store.append(
                run_id=child.id,
                event_type=EventType.RUN_STATUS_CHANGED,
                payload=RunStatusChangedPayload(
                    previous_status=RunStatus.RUNNING,
                    status=RunStatus.PAUSED,
                ),
            )
        return paused

    @staticmethod
    def _ensure_contiguous(events: list[DomainEvent[EventPayload]]) -> None:
        if [event.sequence for event in events] != list(range(1, len(events) + 1)):
            raise ReplayError("Run event sequence is not contiguous")
