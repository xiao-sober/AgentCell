"""Session-scoped repositories returning domain models rather than ORM rows."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentcell.agents import AgentDelegation, AgentSpec, DelegationStatus
from agentcell.changes.models import ChangeSet, FileChange
from agentcell.conversations.models import (
    Conversation,
    ConversationMessage,
    ConversationMessageKind,
    ConversationRoutingMode,
)
from agentcell.errors import (
    AgentRegistrationError,
    ApprovalNotFoundError,
    ChangeNotFoundError,
    CheckpointNotFoundError,
    ConfigurationError,
    ConversationConflictError,
    ConversationModelBindingError,
    ConversationNotFoundError,
    DelegationNotFoundError,
    EventPayloadTooLargeError,
    EventPayloadTypeError,
    InvalidEventCursorError,
    MemoryNotFoundError,
    RunAlreadyExistsError,
    RunNotFoundError,
    StorageIntegrityError,
    StoredEventDataError,
    ToolReplayBlockedError,
)
from agentcell.events import (
    DomainEvent,
    EventPayload,
    EventType,
    JsonValue,
    parse_event_payload,
    payload_model_for,
)
from agentcell.kernel.checkpoint import Checkpoint, CheckpointKind
from agentcell.kernel.models import Run
from agentcell.memory.models import MemoryItem, MemoryKind, MemoryScope
from agentcell.policy import Approval, ApprovalStatus
from agentcell.storage.database import Database
from agentcell.storage.tables import (
    AgentDelegationRow,
    AgentSpecRow,
    ApprovalRow,
    ArtifactRow,
    ChangeSetRow,
    CheckpointRow,
    ConversationMessageRow,
    ConversationRow,
    FileChangeRow,
    MemoryItemRow,
    RunEventRow,
    RunRow,
    ToolExecutionRow,
)
from agentcell.tools import ToolCall, ToolResult
from agentcell.tools.artifacts import ArtifactMetadata

MAX_INLINE_EVENT_PAYLOAD_BYTES = 64 * 1_024


class AgentSpecRepository:
    """Persist user-managed Agent declarations independently of built-ins."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, spec: AgentSpec) -> AgentSpec:
        now = datetime.now(UTC)
        self._session.add(
            AgentSpecRow(
                id=spec.id,
                data=spec.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise AgentRegistrationError(f"Agent {spec.id!r} is already persisted") from exc
        return spec

    async def save(self, spec: AgentSpec) -> AgentSpec:
        now = datetime.now(UTC)
        row = await self._session.get(AgentSpecRow, spec.id)
        if row is None:
            self._session.add(
                AgentSpecRow(
                    id=spec.id,
                    data=spec.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            row.data = spec.model_dump(mode="json")
            row.updated_at = now
        await self._session.flush()
        return spec

    async def list(self) -> list[AgentSpec]:
        rows = (await self._session.scalars(select(AgentSpecRow).order_by(AgentSpecRow.id))).all()
        return [AgentSpec.model_validate(row.data) for row in rows]


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
            execution_identity=(
                None
                if run.execution_identity is None
                else run.execution_identity.model_dump(mode="json")
            ),
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
                execution_identity=(
                    None
                    if run.execution_identity is None
                    else run.execution_identity.model_dump(mode="json")
                ),
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
                "execution_identity": row.execution_identity,
                "parent_run_id": row.parent_run_id,
                "status": row.status,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )


class ConversationRepository:
    """Persist scoped threads and atomically guard their single active root Run."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, conversation: Conversation) -> Conversation:
        self._session.add(
            ConversationRow(
                id=conversation.id,
                user_id=conversation.user_id,
                project_id=conversation.project_id,
                workspace=conversation.workspace,
                agent_id=conversation.agent_id,
                routing_mode=conversation.routing_mode.value,
                team_id=conversation.team_id,
                routing_policy_version=conversation.routing_policy_version,
                model_ref=conversation.model_ref,
                title=conversation.title,
                active_run_id=conversation.active_run_id,
                next_message_sequence=1,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise StorageIntegrityError(f"Could not create Conversation {conversation.id}") from exc
        return conversation

    async def get(self, conversation_id: UUID) -> Conversation | None:
        row = await self._session.get(ConversationRow, conversation_id)
        return None if row is None else self._to_domain(row)

    async def get_required(self, conversation_id: UUID) -> Conversation:
        conversation = await self.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(str(conversation_id))
        return conversation

    async def list_for_user(self, user_id: UUID, *, limit: int = 100) -> list[Conversation]:
        rows = (
            await self._session.scalars(
                select(ConversationRow)
                .where(ConversationRow.user_id == user_id)
                .order_by(ConversationRow.updated_at.desc())
                .limit(limit)
            )
        ).all()
        return [self._to_domain(row) for row in rows]

    async def claim(self, conversation_id: UUID, run_id: UUID) -> Conversation:
        conversation = await self.get_required(conversation_id)
        if conversation.active_run_id is not None:
            active = await self._session.get(RunRow, conversation.active_run_id)
            if active is None or active.status in {"completed", "failed", "cancelled"}:
                await self._session.execute(
                    update(ConversationRow)
                    .where(
                        ConversationRow.id == conversation_id,
                        ConversationRow.active_run_id == conversation.active_run_id,
                    )
                    .values(active_run_id=None)
                )
            else:
                raise ConversationConflictError(
                    f"Conversation {conversation_id} already has active Run "
                    f"{conversation.active_run_id}"
                )
        now = datetime.now(UTC)
        result = await self._session.execute(
            update(ConversationRow)
            .where(
                ConversationRow.id == conversation_id,
                ConversationRow.active_run_id.is_(None),
            )
            .values(active_run_id=run_id, updated_at=now)
            .returning(ConversationRow.id)
        )
        if result.scalar_one_or_none() is None:
            raise ConversationConflictError(
                f"Conversation {conversation_id} already has an active Run"
            )
        claimed = await self.get_required(conversation_id)
        return claimed

    async def release(self, conversation_id: UUID, run_id: UUID) -> None:
        await self._session.execute(
            update(ConversationRow)
            .where(
                ConversationRow.id == conversation_id,
                ConversationRow.active_run_id == run_id,
            )
            .values(active_run_id=None, updated_at=datetime.now(UTC))
        )

    async def bind_model(self, conversation_id: UUID, model_ref: str) -> Conversation:
        """Bind one legacy Conversation exactly once without allowing model drift."""

        await self._session.execute(
            update(ConversationRow)
            .where(
                ConversationRow.id == conversation_id,
                ConversationRow.model_ref.is_(None),
            )
            .values(model_ref=model_ref, updated_at=datetime.now(UTC))
        )
        conversation = await self.get_required(conversation_id)
        if conversation.model_ref != model_ref:
            raise ConversationModelBindingError(
                f"Conversation {conversation_id} is bound to model "
                f"{conversation.model_ref!r}, not {model_ref!r}"
            )
        return conversation

    @staticmethod
    def _to_domain(row: ConversationRow) -> Conversation:
        return Conversation(
            id=row.id,
            user_id=row.user_id,
            project_id=row.project_id,
            workspace=row.workspace,
            agent_id=row.agent_id,
            routing_mode=ConversationRoutingMode(row.routing_mode),
            team_id=row.team_id,
            routing_policy_version=row.routing_policy_version,
            model_ref=row.model_ref,
            title=row.title,
            active_run_id=row.active_run_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class ConversationMessageRepository:
    """Append and query authoritative sanitized Conversation messages."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        conversation_id: UUID,
        run_id: UUID,
        kind: ConversationMessageKind,
        payload: dict[str, JsonValue],
        artifact_ids: tuple[UUID, ...] = (),
    ) -> ConversationMessage:
        next_sequence = await self._session.scalar(
            update(ConversationRow)
            .where(ConversationRow.id == conversation_id)
            .values(
                next_message_sequence=ConversationRow.next_message_sequence + 1,
                updated_at=datetime.now(UTC),
            )
            .returning(ConversationRow.next_message_sequence)
        )
        if next_sequence is None:
            raise ConversationNotFoundError(str(conversation_id))
        message = ConversationMessage(
            conversation_id=conversation_id,
            run_id=run_id,
            sequence=next_sequence - 1,
            kind=kind,
            payload=payload,
            artifact_ids=artifact_ids,
        )
        self._session.add(
            ConversationMessageRow(
                id=message.id,
                conversation_id=message.conversation_id,
                run_id=message.run_id,
                sequence=message.sequence,
                kind=message.kind.value,
                payload_version=message.payload_version,
                payload=message.payload,
                artifact_ids=[str(item) for item in message.artifact_ids],
                created_at=message.created_at,
            )
        )
        await self._session.flush()
        return message

    async def list_for_conversation(
        self,
        conversation_id: UUID,
        *,
        completed_only: bool = False,
        limit: int = 500,
    ) -> list[ConversationMessage]:
        statement = (
            select(ConversationMessageRow)
            .where(ConversationMessageRow.conversation_id == conversation_id)
            .order_by(ConversationMessageRow.sequence.desc())
            .limit(limit)
        )
        if completed_only:
            statement = statement.join(RunRow, RunRow.id == ConversationMessageRow.run_id).where(
                RunRow.status == "completed"
            )
        rows = list((await self._session.scalars(statement)).all())
        rows.reverse()
        return [self._to_domain(row) for row in rows]

    @staticmethod
    def _to_domain(row: ConversationMessageRow) -> ConversationMessage:
        return ConversationMessage(
            id=row.id,
            conversation_id=row.conversation_id,
            run_id=row.run_id,
            sequence=row.sequence,
            kind=ConversationMessageKind(row.kind),
            payload_version=row.payload_version,
            payload=row.payload,
            artifact_ids=tuple(UUID(item) for item in row.artifact_ids),
            created_at=row.created_at,
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


class ApprovalRepository:
    """Persist approval envelopes and their single mutable decision projection."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, approval: Approval) -> Approval:
        self._session.add(
            ApprovalRow(
                id=approval.id,
                run_id=approval.run_id,
                provider_call_id=approval.provider_call_id,
                data=approval.model_dump(mode="json", exclude_computed_fields=True),
                status=approval.status.value,
                created_at=approval.created_at,
                decided_at=approval.decided_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise StorageIntegrityError("Could not create approval") from error
        return approval

    async def get(self, approval_id: UUID) -> Approval | None:
        row = await self._session.get(ApprovalRow, approval_id)
        return None if row is None else Approval.model_validate(row.data)

    async def get_required(self, approval_id: UUID) -> Approval:
        approval = await self.get(approval_id)
        if approval is None:
            raise ApprovalNotFoundError(str(approval_id))
        return approval

    async def find_by_provider_call(
        self,
        run_id: UUID,
        provider_call_id: str,
    ) -> Approval | None:
        row = await self._session.scalar(
            select(ApprovalRow).where(
                ApprovalRow.run_id == run_id,
                ApprovalRow.provider_call_id == provider_call_id,
            )
        )
        return None if row is None else Approval.model_validate(row.data)

    async def list_for_run(
        self, run_id: UUID, *, status: ApprovalStatus | None = None
    ) -> list[Approval]:
        statement = select(ApprovalRow).where(ApprovalRow.run_id == run_id)
        if status is not None:
            statement = statement.where(ApprovalRow.status == status.value)
        rows = (await self._session.scalars(statement.order_by(ApprovalRow.created_at))).all()
        return [Approval.model_validate(row.data) for row in rows]

    async def save(self, approval: Approval) -> Approval:
        result = await self._session.execute(
            update(ApprovalRow)
            .where(ApprovalRow.id == approval.id)
            .values(
                data=approval.model_dump(mode="json", exclude_computed_fields=True),
                status=approval.status.value,
                decided_at=approval.decided_at,
            )
            .returning(ApprovalRow.id)
        )
        if result.scalar_one_or_none() is None:
            raise ApprovalNotFoundError(str(approval.id))
        return approval


class CheckpointRepository:
    """Store immutable restart-safe checkpoints and select the latest snapshot."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, checkpoint: Checkpoint) -> Checkpoint:
        self._session.add(
            CheckpointRow(
                id=checkpoint.id,
                run_id=checkpoint.run_id,
                event_sequence=checkpoint.event_sequence,
                data=checkpoint.model_dump(mode="json", exclude_computed_fields=True),
                created_at=checkpoint.created_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise StorageIntegrityError("Could not create checkpoint") from error
        return checkpoint

    async def latest(self, run_id: UUID, *, through_sequence: int | None = None) -> Checkpoint:
        statement = select(CheckpointRow).where(CheckpointRow.run_id == run_id)
        if through_sequence is not None:
            statement = statement.where(CheckpointRow.event_sequence <= through_sequence)
        row = await self._session.scalar(
            statement.order_by(CheckpointRow.event_sequence.desc()).limit(1)
        )
        if row is None:
            raise CheckpointNotFoundError(str(run_id))
        return Checkpoint.model_validate(row.data)

    async def latest_by_kind(
        self,
        run_id: UUID,
        kind: CheckpointKind,
    ) -> Checkpoint:
        """Return the latest checkpoint of one workflow kind for a Run."""

        rows = await self._session.scalars(
            select(CheckpointRow)
            .where(CheckpointRow.run_id == run_id)
            .order_by(CheckpointRow.event_sequence.desc())
        )
        for row in rows:
            checkpoint = Checkpoint.model_validate(row.data)
            if checkpoint.kind is kind:
                return checkpoint
        raise CheckpointNotFoundError(f"{run_id}:{kind.value}")


class SqliteToolExecutionLedger:
    """Transaction-per-operation durable execution ledger for one Run."""

    def __init__(self, database: Database, run_id: UUID) -> None:
        self._database = database
        self._run_id = run_id

    async def begin(self, call: ToolCall, *, idempotent: bool) -> ToolResult | None:
        if call.provider_call_id is None:
            return None
        async with self._database.transaction() as session:
            row = await session.scalar(
                select(ToolExecutionRow).where(
                    ToolExecutionRow.run_id == self._run_id,
                    ToolExecutionRow.provider_call_id == call.provider_call_id,
                )
            )
            if row is None:
                session.add(
                    ToolExecutionRow(
                        id=call.call_id,
                        run_id=self._run_id,
                        provider_call_id=call.provider_call_id,
                        tool_name=call.tool_name,
                        call_id=call.call_id,
                        idempotent=idempotent,
                        status="started",
                        result=None,
                        started_at=datetime.now(UTC),
                        completed_at=None,
                    )
                )
                await session.flush()
                return None
            if row.status == "completed" and row.result is not None:
                return ToolResult.model_validate(row.result)
            if not row.idempotent:
                raise ToolReplayBlockedError(call.tool_name, call.provider_call_id)
            row.status = "started"
            row.call_id = call.call_id
            row.started_at = datetime.now(UTC)
            row.completed_at = None
            return None

    async def complete(self, call: ToolCall, result: ToolResult) -> None:
        if call.provider_call_id is None:
            return
        async with self._database.transaction() as session:
            row = await session.scalar(
                select(ToolExecutionRow).where(
                    ToolExecutionRow.run_id == self._run_id,
                    ToolExecutionRow.provider_call_id == call.provider_call_id,
                )
            )
            if row is None:
                raise StorageIntegrityError("Tool execution was not claimed")
            row.status = "completed"
            row.result = result.model_dump(mode="json")
            row.completed_at = datetime.now(UTC)

    async def fail(self, call: ToolCall) -> None:
        if call.provider_call_id is None:
            return
        async with self._database.transaction() as session:
            row = await session.scalar(
                select(ToolExecutionRow).where(
                    ToolExecutionRow.run_id == self._run_id,
                    ToolExecutionRow.provider_call_id == call.provider_call_id,
                )
            )
            if row is not None and row.status != "completed":
                row.status = "failed"

    async def complete_deferred(
        self,
        provider_call_id: str,
        output: JsonValue,
    ) -> ToolResult:
        """Complete a durable external call before its PydanticAI continuation resumes."""

        encoded = json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        async with self._database.transaction() as session:
            row = await session.scalar(
                select(ToolExecutionRow).where(
                    ToolExecutionRow.run_id == self._run_id,
                    ToolExecutionRow.provider_call_id == provider_call_id,
                )
            )
            if row is None:
                raise StorageIntegrityError("Deferred tool execution was not claimed")
            result = ToolResult(
                call_id=row.call_id,
                tool_name=row.tool_name,
                output=output,
                output_bytes=len(encoded),
                duration_ms=0,
            )
            row.status = "completed"
            row.result = result.model_dump(mode="json")
            row.completed_at = datetime.now(UTC)
            return result


class AgentDelegationRepository:
    """Persist idempotent parent/child delegation projections."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, delegation: AgentDelegation) -> AgentDelegation:
        self._session.add(
            AgentDelegationRow(
                id=delegation.id,
                parent_run_id=delegation.parent_run_id,
                child_run_id=delegation.child_run_id,
                provider_call_id=delegation.provider_call_id,
                target_agent_id=delegation.target_agent_id,
                kind=delegation.kind.value,
                status=delegation.status.value,
                depth=delegation.depth,
                data=delegation.model_dump(mode="json", exclude_computed_fields=True),
                created_at=delegation.created_at,
                updated_at=delegation.updated_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise StorageIntegrityError("Could not create Agent delegation") from error
        return delegation

    async def get(self, delegation_id: UUID) -> AgentDelegation | None:
        row = await self._session.get(AgentDelegationRow, delegation_id)
        return None if row is None else AgentDelegation.model_validate(row.data)

    async def get_required(self, delegation_id: UUID) -> AgentDelegation:
        delegation = await self.get(delegation_id)
        if delegation is None:
            raise DelegationNotFoundError(str(delegation_id))
        return delegation

    async def find_by_parent_call(
        self,
        parent_run_id: UUID,
        provider_call_id: str,
    ) -> AgentDelegation | None:
        row = await self._session.scalar(
            select(AgentDelegationRow).where(
                AgentDelegationRow.parent_run_id == parent_run_id,
                AgentDelegationRow.provider_call_id == provider_call_id,
            )
        )
        return None if row is None else AgentDelegation.model_validate(row.data)

    async def find_by_child(self, child_run_id: UUID) -> AgentDelegation | None:
        row = await self._session.scalar(
            select(AgentDelegationRow).where(AgentDelegationRow.child_run_id == child_run_id)
        )
        return None if row is None else AgentDelegation.model_validate(row.data)

    async def list_active_for_parent(self, parent_run_id: UUID) -> list[AgentDelegation]:
        rows = await self._session.scalars(
            select(AgentDelegationRow)
            .where(
                AgentDelegationRow.parent_run_id == parent_run_id,
                AgentDelegationRow.status.in_(
                    (
                        DelegationStatus.PENDING.value,
                        DelegationStatus.RUNNING.value,
                        DelegationStatus.WAITING_APPROVAL.value,
                    )
                ),
            )
            .order_by(AgentDelegationRow.created_at)
        )
        return [AgentDelegation.model_validate(row.data) for row in rows]

    async def list_for_parent(self, parent_run_id: UUID) -> list[AgentDelegation]:
        rows = await self._session.scalars(
            select(AgentDelegationRow)
            .where(AgentDelegationRow.parent_run_id == parent_run_id)
            .order_by(AgentDelegationRow.created_at)
        )
        return [AgentDelegation.model_validate(row.data) for row in rows]

    async def save(self, delegation: AgentDelegation) -> AgentDelegation:
        result = await self._session.execute(
            update(AgentDelegationRow)
            .where(AgentDelegationRow.id == delegation.id)
            .values(
                status=delegation.status.value,
                data=delegation.model_dump(mode="json", exclude_computed_fields=True),
                updated_at=delegation.updated_at,
            )
            .returning(AgentDelegationRow.id)
        )
        if result.scalar_one_or_none() is None:
            raise DelegationNotFoundError(str(delegation.id))
        return delegation


class ChangeSetRepository:
    """Persist one lazily-created ChangeSet per Run."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, value: ChangeSet) -> ChangeSet:
        self._session.add(
            ChangeSetRow(
                id=value.id,
                run_id=value.run_id,
                status=value.status.value,
                data=value.model_dump(mode="json"),
                created_at=value.created_at,
                completed_at=value.completed_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise StorageIntegrityError("Could not create ChangeSet") from error
        return value

    async def get(self, change_set_id: UUID) -> ChangeSet | None:
        row = await self._session.get(ChangeSetRow, change_set_id)
        return None if row is None else ChangeSet.model_validate(row.data)

    async def get_for_run(self, run_id: UUID) -> ChangeSet | None:
        row = await self._session.scalar(select(ChangeSetRow).where(ChangeSetRow.run_id == run_id))
        return None if row is None else ChangeSet.model_validate(row.data)

    async def save(self, value: ChangeSet) -> ChangeSet:
        result = await self._session.execute(
            update(ChangeSetRow)
            .where(ChangeSetRow.id == value.id)
            .values(
                status=value.status.value,
                data=value.model_dump(mode="json"),
                completed_at=value.completed_at,
            )
            .returning(ChangeSetRow.id)
        )
        if result.scalar_one_or_none() is None:
            raise StorageIntegrityError("ChangeSet does not exist")
        return value


class FileChangeRepository:
    """Persist and query exact file transitions after process restart."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, value: FileChange) -> FileChange:
        self._session.add(
            FileChangeRow(
                id=value.id,
                change_set_id=value.change_set_id,
                run_id=value.run_id,
                path=value.path,
                provider_call_id=value.provider_call_id,
                status=value.status.value,
                data=value.model_dump(mode="json"),
                created_at=value.created_at,
                completed_at=value.completed_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise StorageIntegrityError("Could not create FileChange") from error
        return value

    async def get(self, change_id: UUID) -> FileChange | None:
        row = await self._session.get(FileChangeRow, change_id)
        return None if row is None else FileChange.model_validate(row.data)

    async def get_required(self, change_id: UUID) -> FileChange:
        value = await self.get(change_id)
        if value is None:
            raise ChangeNotFoundError(str(change_id))
        return value

    async def list_for_run(self, run_id: UUID) -> list[FileChange]:
        rows = await self._session.scalars(
            select(FileChangeRow)
            .where(FileChangeRow.run_id == run_id)
            .order_by(FileChangeRow.created_at, FileChangeRow.id)
        )
        return [FileChange.model_validate(row.data) for row in rows]

    async def save(self, value: FileChange) -> FileChange:
        result = await self._session.execute(
            update(FileChangeRow)
            .where(FileChangeRow.id == value.id)
            .values(
                status=value.status.value,
                data=value.model_dump(mode="json"),
                completed_at=value.completed_at,
            )
            .returning(FileChangeRow.id)
        )
        if result.scalar_one_or_none() is None:
            raise ChangeNotFoundError(str(value.id))
        return value


class ArtifactRepository:
    """Persist and resolve file-backed Artifact metadata."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, metadata: ArtifactMetadata) -> ArtifactMetadata:
        self._session.add(
            ArtifactRow(
                id=metadata.id,
                media_type=metadata.media_type,
                size_bytes=metadata.size_bytes,
                sha256=metadata.sha256,
                storage_key=metadata.storage_key,
                suggested_name=metadata.suggested_name,
                created_at=metadata.created_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise StorageIntegrityError("Could not create Artifact metadata") from error
        return metadata

    async def get(self, artifact_id: UUID) -> ArtifactMetadata | None:
        row = await self._session.get(ArtifactRow, artifact_id)
        return None if row is None else self._artifact_to_domain(row)

    async def find_by_hash(self, sha256: str, size_bytes: int) -> ArtifactMetadata | None:
        row = await self._session.scalar(
            select(ArtifactRow).where(
                ArtifactRow.sha256 == sha256,
                ArtifactRow.size_bytes == size_bytes,
            )
        )
        return None if row is None else self._artifact_to_domain(row)

    @staticmethod
    def _artifact_to_domain(row: ArtifactRow) -> ArtifactMetadata:
        return ArtifactMetadata(
            id=row.id,
            media_type=row.media_type,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            storage_key=row.storage_key,
            suggested_name=row.suggested_name,
            created_at=row.created_at,
        )


class MemoryRepository:
    """CRUD and FTS5 candidate retrieval for scoped MemoryItem models."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, item: MemoryItem) -> MemoryItem:
        row = self._memory_row(item)
        self._session.add(row)
        await self._session.flush()
        return item

    async def get(self, memory_id: UUID) -> MemoryItem | None:
        row = await self._session.get(MemoryItemRow, memory_id)
        return None if row is None else self._memory_to_domain(row)

    async def save(self, item: MemoryItem) -> MemoryItem:
        result = await self._session.execute(
            update(MemoryItemRow)
            .where(MemoryItemRow.id == item.id)
            .values(
                kind=item.kind.value,
                user_id=item.scope.user_id,
                project_id=item.scope.project_id,
                agent_id=item.scope.agent_id,
                content=item.content,
                normalized_hash=self.content_hash(item.content),
                tags=sorted(item.tags),
                importance=item.importance,
                sensitive=item.sensitive,
                created_at=item.created_at,
                updated_at=item.updated_at,
                expires_at=item.expires_at,
            )
            .returning(MemoryItemRow.id)
        )
        if result.scalar_one_or_none() is None:
            raise MemoryNotFoundError
        return item

    async def delete(self, memory_id: UUID) -> bool:
        result = await self._session.scalar(
            delete(MemoryItemRow).where(MemoryItemRow.id == memory_id).returning(MemoryItemRow.id)
        )
        return result is not None

    async def find_duplicate(
        self,
        *,
        kind: MemoryKind,
        scope: MemoryScope,
        content: str,
    ) -> MemoryItem | None:
        row = await self._session.scalar(
            select(MemoryItemRow).where(
                MemoryItemRow.kind == kind.value,
                MemoryItemRow.user_id == scope.user_id,
                MemoryItemRow.project_id == scope.project_id,
                MemoryItemRow.agent_id == scope.agent_id,
                MemoryItemRow.normalized_hash == self.content_hash(content),
            )
        )
        return None if row is None else self._memory_to_domain(row)

    async def search_fts(
        self,
        query: str,
        *,
        scope: MemoryScope,
        limit: int,
    ) -> list[tuple[MemoryItem, float]]:
        tokens = re.findall(r"[\w-]+", query.casefold(), flags=re.UNICODE)
        if not tokens:
            return []
        fts_query = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        agent_filter = (
            "m.agent_id IS NULL"
            if scope.agent_id is None
            else "(m.agent_id IS NULL OR m.agent_id = :agent_id)"
        )
        statement = text(
            "SELECT m.id AS id, bm25(memory_fts) AS rank "
            "FROM memory_fts JOIN memory_items AS m ON memory_fts.memory_id = m.id "
            "WHERE memory_fts MATCH :query AND m.user_id = :user_id "
            "AND m.project_id = :project_id AND "
            f"{agent_filter} ORDER BY rank LIMIT :limit"
        )
        rows = (
            await self._session.execute(
                statement,
                {
                    "query": fts_query,
                    "user_id": scope.user_id.hex,
                    "project_id": scope.project_id,
                    "agent_id": scope.agent_id,
                    "limit": limit,
                },
            )
        ).mappings()
        output: list[tuple[MemoryItem, float]] = []
        for row in rows:
            item = await self.get(UUID(str(row["id"])))
            if item is not None:
                output.append((item, float(row["rank"])))
        return output

    @staticmethod
    def content_hash(content: str) -> str:
        normalized = " ".join(content.casefold().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @classmethod
    def _memory_row(cls, item: MemoryItem) -> MemoryItemRow:
        return MemoryItemRow(
            id=item.id,
            kind=item.kind.value,
            user_id=item.scope.user_id,
            project_id=item.scope.project_id,
            agent_id=item.scope.agent_id,
            content=item.content,
            normalized_hash=cls.content_hash(item.content),
            tags=sorted(item.tags),
            importance=item.importance,
            sensitive=item.sensitive,
            created_at=item.created_at,
            updated_at=item.updated_at,
            expires_at=item.expires_at,
        )

    @staticmethod
    def _memory_to_domain(row: MemoryItemRow) -> MemoryItem:
        return MemoryItem(
            id=row.id,
            kind=MemoryKind(row.kind),
            scope=MemoryScope(
                user_id=row.user_id,
                project_id=row.project_id,
                agent_id=row.agent_id,
            ),
            content=row.content,
            tags=frozenset(row.tags),
            importance=row.importance,
            sensitive=row.sensitive,
            created_at=row.created_at,
            updated_at=row.updated_at,
            expires_at=row.expires_at,
        )
