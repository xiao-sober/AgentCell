"""Memory CRUD, policy evaluation, deduplication, expiry, and ranked retrieval."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from agentcell.errors import MemoryNotFoundError
from agentcell.events import EventPayload, EventType, GenericEventPayload
from agentcell.memory.models import (
    MemoryCandidate,
    MemoryItem,
    MemoryScope,
    MemorySearchResult,
)
from agentcell.memory.policy import MemoryPolicy
from agentcell.storage.database import Database
from agentcell.storage.repositories import MemoryRepository


class MemoryEventSink(Protocol):
    async def emit(self, event_type: EventType, payload: EventPayload) -> None: ...


class MemoryService:
    """Single application boundary for long-term memory reads and writes."""

    def __init__(
        self,
        database: Database,
        *,
        policy: MemoryPolicy | None = None,
        events: MemoryEventSink | None = None,
    ) -> None:
        self._database = database
        self._policy = policy or MemoryPolicy()
        self._events = events

    async def remember(
        self,
        candidate: MemoryCandidate,
        *,
        approval_granted: bool = False,
    ) -> MemoryItem:
        decision = self._policy.evaluate(candidate, approval_granted=approval_granted)
        async with self._database.transaction() as session:
            repository = MemoryRepository(session)
            duplicate = await repository.find_duplicate(
                kind=candidate.kind,
                scope=candidate.scope,
                content=candidate.content,
            )
            if duplicate is not None and not duplicate.is_expired():
                return duplicate
            item = MemoryItem(
                kind=candidate.kind,
                scope=candidate.scope,
                content=candidate.content,
                tags=candidate.tags,
                importance=candidate.importance,
                sensitive=decision.sensitive,
                expires_at=candidate.expires_at,
            )
            created = await repository.create(item)
        if self._events is not None:
            await self._events.emit(
                EventType.MEMORY_WRITTEN,
                GenericEventPayload(
                    data={"memory_id": str(created.id), "kind": created.kind.value}
                ),
            )
        return created

    async def update(
        self,
        memory_id: UUID,
        *,
        scope: MemoryScope,
        content: str,
        tags: frozenset[str],
        importance: float,
        expires_at: datetime | None = None,
        approval_granted: bool = False,
    ) -> MemoryItem:
        async with self._database.transaction() as session:
            repository = MemoryRepository(session)
            existing = await repository.get(memory_id)
            if existing is None or existing.scope != scope:
                raise MemoryNotFoundError("Memory was not found in this scope")
            candidate = MemoryCandidate(
                kind=existing.kind,
                scope=scope,
                content=content,
                tags=tags,
                importance=importance,
                expires_at=expires_at,
            )
            decision = self._policy.evaluate(
                candidate,
                approval_granted=approval_granted,
            )
            updated = MemoryItem(
                id=existing.id,
                kind=existing.kind,
                scope=scope,
                content=content,
                tags=tags,
                importance=importance,
                sensitive=decision.sensitive,
                created_at=existing.created_at,
                updated_at=datetime.now(UTC),
                expires_at=expires_at,
            )
            return await repository.save(updated)

    async def forget(self, memory_id: UUID, *, scope: MemoryScope) -> bool:
        async with self._database.transaction() as session:
            repository = MemoryRepository(session)
            existing = await repository.get(memory_id)
            if existing is None or existing.scope != scope:
                return False
            return await repository.delete(memory_id)

    async def search(
        self,
        query: str,
        *,
        scope: MemoryScope,
        tags: frozenset[str] = frozenset(),
        limit: int = 10,
        now: datetime | None = None,
    ) -> tuple[MemorySearchResult, ...]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        async with self._database.session() as session:
            candidates = await MemoryRepository(session).search_fts(
                query,
                scope=scope,
                limit=min(500, limit * 10),
            )
        active = [(item, rank) for item, rank in candidates if not item.is_expired(at=current)]
        raw_relevance = [max(0.0, -rank) for _, rank in active]
        max_relevance = max(raw_relevance, default=0.0)
        results: list[MemorySearchResult] = []
        for (item, _), raw in zip(active, raw_relevance, strict=True):
            relevance = raw / max_relevance if max_relevance > 0 else 0.0
            age_days = max(0.0, (current - item.updated_at).total_seconds() / 86_400)
            time_decay = math.pow(0.5, age_days / 30)
            overlap = len(tags & item.tags) / len(tags) if tags else 0.0
            score = 0.55 * relevance + 0.2 * item.importance + 0.2 * time_decay + 0.05 * overlap
            results.append(
                MemorySearchResult(
                    item=item,
                    score=score,
                    bm25_relevance=relevance,
                    time_decay=time_decay,
                    tag_overlap=overlap,
                )
            )
        results.sort(key=lambda result: (-result.score, -result.item.updated_at.timestamp()))
        selected = tuple(results[:limit])
        if self._events is not None and selected:
            await self._events.emit(
                EventType.MEMORY_RECALLED,
                GenericEventPayload(
                    data={
                        "query": query,
                        "memory_ids": [str(result.item.id) for result in selected],
                    }
                ),
            )
        return selected
