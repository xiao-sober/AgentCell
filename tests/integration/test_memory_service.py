"""Stage 7 memory policy, FTS5 ranking, scope isolation, dedup, and expiry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agentcell.errors import MemoryApprovalRequiredError, MemoryPolicyRejectedError
from agentcell.memory.models import MemoryCandidate, MemoryItem, MemoryKind, MemoryScope
from agentcell.memory.service import MemoryService
from agentcell.storage import Database, MemoryRepository


@pytest.mark.asyncio
async def test_memory_dedup_update_delete_and_policy(database: Database) -> None:
    scope = MemoryScope(user_id=uuid4(), project_id="agentcell", agent_id="coder")
    service = MemoryService(database)
    candidate = MemoryCandidate(
        kind=MemoryKind.EPISODIC,
        scope=scope,
        content="Pyright requires strict type annotations",
        tags=frozenset({"python"}),
        importance=0.7,
    )
    first = await service.remember(candidate)
    duplicate = await service.remember(candidate)
    assert duplicate.id == first.id

    updated = await service.update(
        first.id,
        scope=scope,
        content="Pyright and Ruff are required",
        tags=frozenset({"python", "quality"}),
        importance=0.9,
    )
    assert updated.updated_at >= first.updated_at
    assert await service.forget(first.id, scope=scope)
    assert not await service.forget(first.id, scope=scope)

    with pytest.raises(MemoryApprovalRequiredError):
        await service.remember(
            MemoryCandidate(
                kind=MemoryKind.SEMANTIC,
                scope=scope,
                content="The user prefers concise reports",
            )
        )
    semantic = await service.remember(
        MemoryCandidate(
            kind=MemoryKind.SEMANTIC,
            scope=scope,
            content="The user prefers concise reports",
        ),
        approval_granted=True,
    )
    assert semantic.kind is MemoryKind.SEMANTIC

    with pytest.raises(MemoryPolicyRejectedError):
        await service.remember(
            MemoryCandidate(
                kind=MemoryKind.EPISODIC,
                scope=scope,
                content="password=do-not-store",
            )
        )


@pytest.mark.asyncio
async def test_fts_ranking_scope_isolation_and_expiry(database: Database) -> None:
    user_id = uuid4()
    scope = MemoryScope(user_id=user_id, project_id="agentcell", agent_id="coder")
    other_scope = MemoryScope(user_id=uuid4(), project_id="agentcell", agent_id="coder")
    now = datetime.now(UTC)
    async with database.transaction() as session:
        repository = MemoryRepository(session)
        important = await repository.create(
            MemoryItem(
                kind=MemoryKind.EPISODIC,
                scope=scope,
                content="SQLite migration recovery decision",
                tags=frozenset({"database"}),
                importance=1,
                created_at=now,
                updated_at=now,
            )
        )
        await repository.create(
            MemoryItem(
                kind=MemoryKind.EPISODIC,
                scope=scope,
                content="SQLite migration note",
                importance=0.1,
                created_at=now - timedelta(days=90),
                updated_at=now - timedelta(days=90),
            )
        )
        await repository.create(
            MemoryItem(
                kind=MemoryKind.EPISODIC,
                scope=other_scope,
                content="SQLite migration secret other user",
                importance=1,
            )
        )
        await repository.create(
            MemoryItem(
                kind=MemoryKind.WORKING,
                scope=scope,
                content="SQLite migration expired",
                expires_at=now - timedelta(seconds=1),
            )
        )

    results = await MemoryService(database).search(
        "SQLite migration",
        scope=scope,
        tags=frozenset({"database"}),
        now=now,
    )

    assert results
    assert results[0].item.id == important.id
    assert all(result.item.scope.user_id == user_id for result in results)
    assert all(not result.item.is_expired(at=now) for result in results)
