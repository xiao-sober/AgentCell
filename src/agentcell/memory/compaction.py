"""Pair-safe history trimming and Artifact-backed tool output compaction."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)

from agentcell.events import ArtifactReference, EventPayload, EventType, GenericEventPayload
from agentcell.memory.models import MemoryScope


def _estimate_one_message_tokens(message: ModelMessage) -> int:
    serialized = ModelMessagesTypeAdapter.dump_json([message])
    # Three UTF-8 bytes per token is deliberately conservative across prose,
    # source code, JSON, and CJK text. Include a small per-message framing cost.
    return max(1, (len(serialized) + 2) // 3 + 8)


def estimate_message_tokens(messages: Sequence[ModelMessage]) -> int:
    """Estimate Provider-neutral context tokens without requiring a vendor tokenizer."""

    return sum(_estimate_one_message_tokens(message) for message in messages)


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


class PairSafeTokenTrimmer:
    """Trim estimated context tokens while treating tool call/return pairs atomically."""

    def __init__(self, max_tokens: int, *, preserve_first: bool = True) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self._max_tokens = max_tokens
        self._preserve_first = preserve_first

    def trim(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        if estimate_message_tokens(messages) <= self._max_tokens:
            return list(messages)
        groups = self._atomic_groups(messages)
        ordered_groups = sorted(groups, key=max, reverse=True)
        selected: set[int] = set()
        selected_tokens = 0

        def add_group(group: set[int], *, required: bool = False) -> None:
            nonlocal selected_tokens
            new_indices = group - selected
            if not new_indices:
                return
            group_tokens = sum(
                _estimate_one_message_tokens(messages[index]) for index in new_indices
            )
            if not required and selected_tokens + group_tokens > self._max_tokens:
                return
            selected.update(new_indices)
            selected_tokens += group_tokens

        if self._preserve_first and messages:
            add_group(next(group for group in groups if 0 in group), required=True)
        if ordered_groups:
            # Always retain the newest atomic unit. Required anchors and pairs may
            # exceed the estimate in pathological single-message cases.
            add_group(ordered_groups[0], required=True)
        for group in ordered_groups[1:]:
            add_group(group)
        return [message for index, message in enumerate(messages) if index in selected]

    @staticmethod
    def _atomic_groups(messages: list[ModelMessage]) -> list[set[int]]:
        parent = list(range(len(messages)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

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
        for call_id, call_index in call_indices.items():
            return_index = return_indices.get(call_id)
            if return_index is not None:
                union(call_index, return_index)

        grouped: dict[int, set[int]] = {}
        for index in range(len(messages)):
            grouped.setdefault(find(index), set()).add(index)
        return list(grouped.values())


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
    """Apply message, Artifact, and token compaction before each model request."""

    def __init__(
        self,
        artifacts: ArtifactWriter,
        *,
        max_messages: int = 100,
        max_context_tokens: int = 32_000,
        max_tool_output_bytes: int = 8_192,
        memory_injector: ScopedMemoryInjector | None = None,
        memory_scope: MemoryScope | None = None,
        memory_query: str | None = None,
    ) -> None:
        self._message_trimmer = PairSafeTrimmer(max_messages)
        self._token_trimmer = PairSafeTokenTrimmer(max_context_tokens)
        self._max_messages = max_messages
        self._max_context_tokens = max_context_tokens
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
        estimated_tokens_before = estimate_message_tokens(prepared)
        message_trimmed = self._message_trimmer.trim(prepared)
        compacted = await self._compactor.compact(message_trimmed)
        token_trimmed = self._token_trimmer.trim(compacted)
        if token_trimmed != prepared:
            await events.emit(
                EventType.CONTEXT_COMPACTED,
                GenericEventPayload(
                    data={
                        "messages_before": len(prepared),
                        "messages_after": len(token_trimmed),
                        "estimated_tokens_before": estimated_tokens_before,
                        "estimated_tokens_after": estimate_message_tokens(token_trimmed),
                        "max_messages": self._max_messages,
                        "max_estimated_tokens": self._max_context_tokens,
                    }
                ),
            )
        return token_trimmed
