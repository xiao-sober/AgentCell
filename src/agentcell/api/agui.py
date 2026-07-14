"""Pure mapping from persisted domain events to official AG-UI events."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ag_ui.core import (
    BaseEvent,
    CustomEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

from agentcell.events import (
    DomainEvent,
    ErrorPayload,
    EventPayload,
    EventType,
    GenericEventPayload,
    ModelCompletedPayload,
    ModelRequestedPayload,
    RunCompletedPayload,
    RunStartedPayload,
    TextDeltaPayload,
)


@dataclass(slots=True)
class AgUiMappingState:
    thread_id: str | None = None
    active_message_id: str | None = None
    last_message_id: str | None = None


class AgUiEventMapper:
    """Map one ordered Run event stream without querying transport state."""

    def map(
        self,
        event: DomainEvent[EventPayload],
        state: AgUiMappingState,
    ) -> tuple[BaseEvent, ...]:
        timestamp = int(event.occurred_at.timestamp() * 1_000)
        payload = event.payload
        if event.event_type is EventType.RUN_STARTED and isinstance(payload, RunStartedPayload):
            state.thread_id = str(payload.conversation_id)
            return (
                RunStartedEvent(
                    thread_id=str(payload.conversation_id),
                    run_id=str(event.run_id),
                    timestamp=timestamp,
                ),
            )
        if event.event_type is EventType.MODEL_REQUESTED and isinstance(
            payload, ModelRequestedPayload
        ):
            message_id = f"{event.run_id}:message:{payload.request_index}"
            state.active_message_id = message_id
            state.last_message_id = message_id
            return (
                TextMessageStartEvent(
                    message_id=message_id,
                    role="assistant",
                    timestamp=timestamp,
                ),
            )
        if event.event_type is EventType.MODEL_TEXT_DELTA and isinstance(payload, TextDeltaPayload):
            if state.active_message_id is None:
                return self._custom(event, timestamp)
            return (
                TextMessageContentEvent(
                    message_id=state.active_message_id,
                    delta=payload.delta,
                    timestamp=timestamp,
                ),
            )
        if event.event_type is EventType.MODEL_COMPLETED and isinstance(
            payload, ModelCompletedPayload
        ):
            message_id = state.active_message_id or (
                f"{event.run_id}:message:{payload.request_index}"
            )
            state.active_message_id = None
            state.last_message_id = message_id
            return (TextMessageEndEvent(message_id=message_id, timestamp=timestamp),)
        if event.event_type is EventType.TOOL_PROPOSED and isinstance(payload, GenericEventPayload):
            call_id = str(payload.data.get("provider_call_id") or payload.data.get("call_id"))
            name = str(payload.data.get("tool_name") or "unknown")
            arguments = json.dumps(
                payload.data.get("arguments", {}),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return (
                ToolCallStartEvent(
                    tool_call_id=call_id,
                    tool_call_name=name,
                    parent_message_id=state.last_message_id,
                    timestamp=timestamp,
                ),
                ToolCallArgsEvent(
                    tool_call_id=call_id,
                    delta=arguments,
                    timestamp=timestamp,
                ),
                ToolCallEndEvent(tool_call_id=call_id, timestamp=timestamp),
            )
        if event.event_type is EventType.TOOL_STARTED and isinstance(payload, GenericEventPayload):
            return self._custom(event, timestamp)
        if event.event_type is EventType.TOOL_COMPLETED and isinstance(
            payload, GenericEventPayload
        ):
            call_id = str(payload.data.get("provider_call_id") or payload.data.get("call_id"))
            content = json.dumps(payload.data, ensure_ascii=False, separators=(",", ":"))
            return (
                ToolCallResultEvent(
                    message_id=f"{event.run_id}:tool-result:{call_id}",
                    tool_call_id=call_id,
                    content=content,
                    role="tool",
                    timestamp=timestamp,
                ),
            )
        if event.event_type is EventType.RUN_COMPLETED and isinstance(payload, RunCompletedPayload):
            return (
                RunFinishedEvent(
                    thread_id=state.thread_id or str(event.run_id),
                    run_id=str(event.run_id),
                    result=payload.model_dump(mode="json"),
                    timestamp=timestamp,
                ),
            )
        if event.event_type is EventType.RUN_FAILED and isinstance(payload, ErrorPayload):
            return (
                RunErrorEvent(
                    message=payload.message,
                    code=payload.code,
                    timestamp=timestamp,
                ),
            )
        if event.event_type is EventType.RUN_CANCELLED:
            return (
                RunFinishedEvent(
                    thread_id=state.thread_id or str(event.run_id),
                    run_id=str(event.run_id),
                    result={"status": "cancelled"},
                    timestamp=timestamp,
                ),
            )
        return self._custom(event, timestamp)

    @staticmethod
    def _custom(
        event: DomainEvent[EventPayload],
        timestamp: int,
    ) -> tuple[BaseEvent, ...]:
        return (
            CustomEvent(
                name=event.event_type.value,
                value=event.payload.safe_dump(),
                timestamp=timestamp,
            ),
        )
