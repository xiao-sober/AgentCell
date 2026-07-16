"""Safe live projection of delegated child activity onto an authoritative root Run."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from pydantic_ai.messages import ModelMessage

from agentcell.agents import AgentDelegation, DelegationResult
from agentcell.budgets import BudgetSnapshot, BudgetTracker, Usage
from agentcell.events import (
    DomainEvent,
    ErrorPayload,
    EventPayload,
    EventType,
    GenericEventPayload,
    JsonValue,
    TextDeltaPayload,
)
from agentcell.kernel.models import Run
from agentcell.kernel.run_service import RunService
from agentcell.storage import Database, EventStore

_PROJECTION_BATCH_SIZE = 16
_PROJECTION_ACTIVE_DELAY_SECONDS = 0.02
_PROJECTION_IDLE_DELAY_SECONDS = 0.05

_TOOL_EVENTS = frozenset(
    {
        EventType.TOOL_PROPOSED,
        EventType.TOOL_APPROVAL_REQUIRED,
        EventType.TOOL_APPROVED,
        EventType.TOOL_REJECTED,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
        EventType.TOOL_FAILED,
    }
)
_TOOL_DATA_FIELDS: dict[EventType, frozenset[str]] = {
    EventType.TOOL_PROPOSED: frozenset(
        {"call_id", "provider_call_id", "tool_name", "arguments"}
    ),
    EventType.TOOL_APPROVAL_REQUIRED: frozenset(
        {
            "approval_id",
            "provider_call_id",
            "tool_name",
            "risk",
            "arguments",
            "impact",
            "idempotent",
            "timeout_seconds",
        }
    ),
    EventType.TOOL_APPROVED: frozenset(
        {"approval_id", "provider_call_id", "tool_name", "decision_source"}
    ),
    EventType.TOOL_REJECTED: frozenset(
        {"approval_id", "provider_call_id", "tool_name", "decision_source"}
    ),
    EventType.TOOL_STARTED: frozenset(
        {"call_id", "provider_call_id", "tool_name", "timeout_seconds"}
    ),
    EventType.TOOL_COMPLETED: frozenset(
        {
            "call_id",
            "provider_call_id",
            "tool_name",
            "output_bytes",
            "truncated",
            "duration_ms",
            "replayed",
        }
    ),
}


class ChildEventProjector:
    """Relay bounded public child events without duplicating tool output content."""

    def __init__(self, database: Database, runs: RunService) -> None:
        self._database = database
        self._runs = runs

    async def recover(
        self,
        root: Run,
        delegation: AgentDelegation,
        *,
        workspace: Path,
        user_id: UUID,
        conversation_id: UUID,
        message_history: Sequence[ModelMessage] = (),
        project_text: bool = True,
        root_budget: BudgetSnapshot | None = None,
        accounted_child_usage: Usage | None = None,
    ) -> tuple[DelegationResult, bool]:
        """Recover one child while relaying ordered, restart-safe public activity."""

        async with self._database.session() as session:
            root_events = await EventStore(session).list_for_run(root.id)
        cursor = 0
        projected_text = False
        for event in root_events:
            if isinstance(event.payload, TextDeltaPayload):
                if event.payload.source_run_id == delegation.child_run_id:
                    cursor = max(cursor, event.payload.source_sequence or 0)
                    projected_text = True
            source_sequence = self._source_sequence(
                event,
                child_run_id=delegation.child_run_id,
            )
            if source_sequence is not None:
                cursor = max(cursor, source_sequence)

        if message_history:
            recovery_call = self._runs.recover_delegation_child(
                delegation,
                workspace=workspace,
                user_id=user_id,
                conversation_id=conversation_id,
                message_history=message_history,
            )
        else:
            recovery_call = self._runs.recover_delegation_child(
                delegation,
                workspace=workspace,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        recovery = asyncio.create_task(recovery_call)

        async def project_available() -> bool:
            nonlocal cursor, projected_text
            async with self._database.session() as session:
                child_events = await EventStore(session).list_for_run(
                    delegation.child_run_id,
                    after_sequence=cursor,
                )
            if not child_events:
                return False
            child_events = child_events[:_PROJECTION_BATCH_SIZE]
            async with self._database.transaction() as session:
                events = EventStore(session)
                for event in child_events:
                    cursor = event.sequence
                    if (
                        project_text
                        and event.event_type is EventType.MODEL_TEXT_DELTA
                        and isinstance(event.payload, TextDeltaPayload)
                    ):
                        await events.append(
                            run_id=root.id,
                            event_type=EventType.MODEL_TEXT_DELTA,
                            payload=TextDeltaPayload(
                                delta=event.payload.delta,
                                source_run_id=delegation.child_run_id,
                                source_agent_id=delegation.target_agent_id,
                                source_sequence=event.sequence,
                            ),
                        )
                        projected_text = True
                    elif event.event_type in _TOOL_EVENTS:
                        await events.append(
                            run_id=root.id,
                            event_type=event.event_type,
                            payload=self._tool_payload(event, delegation=delegation),
                        )
                    elif event.event_type is EventType.BUDGET_UPDATED and root_budget is not None:
                        payload = self._budget_payload(
                            event,
                            delegation=delegation,
                            root_budget=root_budget,
                            accounted_child_usage=accounted_child_usage or Usage(),
                        )
                        if payload is not None:
                            await events.append(
                                run_id=root.id,
                                event_type=EventType.BUDGET_UPDATED,
                                payload=payload,
                            )
                    elif project_text and event.event_type is EventType.MODEL_OUTPUT_REJECTED:
                        await events.append(
                            run_id=root.id,
                            event_type=EventType.MODEL_OUTPUT_REJECTED,
                            payload=GenericEventPayload(
                                data={
                                    "reason": "routed_child_output_reset",
                                    "source_run_id": str(delegation.child_run_id),
                                    "source_agent_id": delegation.target_agent_id,
                                    "source_sequence": event.sequence,
                                }
                            ),
                        )
            return True

        try:
            while True:
                projected = await project_available()
                if recovery.done() and not projected:
                    return await recovery, projected_text
                await asyncio.sleep(
                    _PROJECTION_ACTIVE_DELAY_SECONDS
                    if projected
                    else _PROJECTION_IDLE_DELAY_SECONDS
                )
        except BaseException:
            if not recovery.done():
                recovery.cancel()
                await asyncio.gather(recovery, return_exceptions=True)
            raise

    @staticmethod
    def _source_sequence(
        event: DomainEvent[EventPayload],
        *,
        child_run_id: UUID,
    ) -> int | None:
        safe = event.safe_payload()
        for container in (safe.get("data"), safe.get("details")):
            if not isinstance(container, dict):
                continue
            source_run_id = container.get("source_run_id") or container.get("child_run_id")
            source_sequence = container.get("source_sequence") or container.get(
                "child_sequence"
            )
            if source_run_id == str(child_run_id) and isinstance(source_sequence, int):
                return source_sequence
        return None

    @staticmethod
    def _tool_payload(
        event: DomainEvent[EventPayload],
        *,
        delegation: AgentDelegation,
    ) -> EventPayload:
        provenance: dict[str, JsonValue] = {
            "source_run_id": str(delegation.child_run_id),
            "source_agent_id": delegation.target_agent_id,
            "source_sequence": event.sequence,
        }
        if event.event_type is EventType.TOOL_FAILED:
            if not isinstance(event.payload, ErrorPayload):
                raise TypeError("tool.failed requires ErrorPayload")
            return ErrorPayload(
                code=event.payload.code,
                message=event.payload.message,
                retryable=event.payload.retryable,
                details={**event.payload.details, **provenance},
            )
        safe_data = event.safe_payload().get("data")
        if not isinstance(safe_data, dict):
            raise TypeError(f"{event.event_type.value} requires GenericEventPayload")
        allowed = _TOOL_DATA_FIELDS.get(event.event_type, frozenset())
        projected = {key: value for key, value in safe_data.items() if key in allowed}
        return GenericEventPayload(data={**projected, **provenance})

    @classmethod
    def _budget_payload(
        cls,
        event: DomainEvent[EventPayload],
        *,
        delegation: AgentDelegation,
        root_budget: BudgetSnapshot,
        accounted_child_usage: Usage,
    ) -> GenericEventPayload | None:
        data = event.safe_payload().get("data")
        if not isinstance(data, dict):
            return None
        snapshot_data = data.get("snapshot")
        if not isinstance(snapshot_data, dict):
            return None
        used_data = snapshot_data.get("used")
        if not isinstance(used_data, dict):
            return None
        child_usage = Usage.model_validate(
            {key: value for key, value in used_data.items() if key in Usage.model_fields}
        )
        tracker = BudgetTracker(root_budget.budget, initial_usage=root_budget.used)
        tracker.record_child_usage(
            cls._usage_delta(child_usage, accounted_child_usage)
        )
        snapshot = tracker.snapshot()
        return GenericEventPayload(
            data={
                "source": "child_progress",
                "source_run_id": str(delegation.child_run_id),
                "source_agent_id": delegation.target_agent_id,
                "source_sequence": event.sequence,
                "snapshot": snapshot.model_dump(mode="json"),
            }
        )

    @staticmethod
    def _usage_delta(current: Usage, accounted: Usage) -> Usage:
        return Usage(
            requests=max(0, current.requests - accounted.requests),
            input_tokens=max(0, current.input_tokens - accounted.input_tokens),
            cache_write_tokens=max(
                0,
                current.cache_write_tokens - accounted.cache_write_tokens,
            ),
            cache_read_tokens=max(
                0,
                current.cache_read_tokens - accounted.cache_read_tokens,
            ),
            output_tokens=max(0, current.output_tokens - accounted.output_tokens),
            tool_calls=max(0, current.tool_calls - accounted.tool_calls),
            cost=max(Decimal("0"), current.cost - accounted.cost),
            children=max(0, current.children - accounted.children),
            max_depth_reached=max(
                0,
                current.max_depth_reached - accounted.max_depth_reached,
            ),
        )
