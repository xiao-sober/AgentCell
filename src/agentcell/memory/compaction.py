"""Pair-safe history trimming and Artifact-backed tool output compaction."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Protocol

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)

from agentcell.events import ArtifactReference, EventPayload, EventType, GenericEventPayload
from agentcell.memory.models import MemoryScope


class ArtifactWriter(Protocol):
    async def save(
        self,
        content: bytes,
        *,
        media_type: str,
        suggested_name: str,
    ) -> ArtifactReference: ...


class ContextEventSink(Protocol):
    async def emit(self, event_type: EventType, payload: EventPayload) -> None: ...


class ScopedMemoryInjector(Protocol):
    async def inject(
        self,
        messages: list[ModelMessage],
        *,
        query: str,
        scope: MemoryScope,
        tags: frozenset[str] = frozenset(),
    ) -> list[ModelMessage]: ...


class PairSafeTrimmer:
    """Trim by message count while retaining both sides of every selected tool pair."""

    def __init__(self, max_messages: int, *, preserve_first: bool = True) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be positive")
        self._max_messages = max_messages
        self._preserve_first = preserve_first

    def trim(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        if len(messages) <= self._max_messages:
            return list(messages)
        selected = set(range(max(0, len(messages) - self._max_messages), len(messages)))
        if self._preserve_first:
            selected.add(0)
        call_indices: dict[str, int] = {}
        return_indices: dict[str, int] = {}
        for index, message in enumerate(messages):
            if isinstance(message, ModelResponse):
                for part in message.parts:
                    if isinstance(part, ToolCallPart):
                        call_indices[part.tool_call_id] = index
            else:
                for part in message.parts:
                    if isinstance(part, ToolReturnPart):
                        return_indices[part.tool_call_id] = index
        changed = True
        while changed:
            changed = False
            for call_id, call_index in call_indices.items():
                return_index = return_indices.get(call_id)
                if return_index is None:
                    continue
                if call_index in selected and return_index not in selected:
                    selected.add(return_index)
                    changed = True
                if return_index in selected and call_index not in selected:
                    selected.add(call_index)
                    changed = True
        return [message for index, message in enumerate(messages) if index in selected]


class ToolOutputCompactor:
    """Replace oversized ToolReturn content with a verified Artifact reference."""

    def __init__(self, artifacts: ArtifactWriter, *, max_inline_bytes: int = 8_192) -> None:
        if max_inline_bytes < 1:
            raise ValueError("max_inline_bytes must be positive")
        self._artifacts = artifacts
        self._max_inline_bytes = max_inline_bytes

    async def compact(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        compacted: list[ModelMessage] = []
        for message in messages:
            if not isinstance(message, ModelRequest):
                compacted.append(message)
                continue
            parts: list[ModelRequestPart] = []
            for part in message.parts:
                if not isinstance(part, ToolReturnPart):
                    parts.append(part)
                    continue
                encoded = json.dumps(
                    part.content,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                if len(encoded) <= self._max_inline_bytes:
                    parts.append(part)
                    continue
                reference = await self._artifacts.save(
                    encoded,
                    media_type="application/json",
                    suggested_name=f"tool-output-{part.tool_call_id}.json",
                )
                parts.append(
                    replace(
                        part,
                        content={
                            "summary": (
                                f"Tool output moved to Artifact ({reference.size_bytes} bytes)."
                            ),
                            "artifact": reference.model_dump(mode="json"),
                        },
                    )
                )
            compacted.append(replace(message, parts=parts))
        return compacted


class ContextManager:
    """Apply pair-safe trimming then Artifact compaction before each model request."""

    def __init__(
        self,
        artifacts: ArtifactWriter,
        *,
        max_messages: int = 100,
        max_tool_output_bytes: int = 8_192,
        memory_injector: ScopedMemoryInjector | None = None,
        memory_scope: MemoryScope | None = None,
        memory_query: str | None = None,
    ) -> None:
        self._trimmer = PairSafeTrimmer(max_messages)
        self._compactor = ToolOutputCompactor(
            artifacts,
            max_inline_bytes=max_tool_output_bytes,
        )
        self._memory_injector = memory_injector
        self._memory_scope = memory_scope
        self._memory_query = memory_query

    async def prepare(
        self,
        messages: list[ModelMessage],
        *,
        events: ContextEventSink,
    ) -> list[ModelMessage]:
        prepared = messages
        if (
            self._memory_injector is not None
            and self._memory_scope is not None
            and self._memory_query
        ):
            prepared = await self._memory_injector.inject(
                messages,
                query=self._memory_query,
                scope=self._memory_scope,
            )
        trimmed = self._trimmer.trim(prepared)
        compacted = await self._compactor.compact(trimmed)
        if compacted != prepared:
            await events.emit(
                EventType.CONTEXT_COMPACTED,
                GenericEventPayload(
                    data={
                        "messages_before": len(prepared),
                        "messages_after": len(compacted),
                    }
                ),
            )
        return compacted
