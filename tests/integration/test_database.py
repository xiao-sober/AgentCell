from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from agentcell.errors import ConfigurationError
from agentcell.storage import Database


@pytest.mark.asyncio
async def test_database_enables_required_sqlite_pragmas(database: Database) -> None:
    async with database.session() as session:
        journal_mode = await session.scalar(text("PRAGMA journal_mode"))
        foreign_keys = await session.scalar(text("PRAGMA foreign_keys"))
        busy_timeout = await session.scalar(text("PRAGMA busy_timeout"))

    assert str(journal_mode).casefold() == "wal"
    assert foreign_keys == 1
    assert busy_timeout == 5_000


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_error(database: Database) -> None:
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO runs "
                    "(id, conversation_id, agent_id, status, created_at, updated_at) "
                    "VALUES (:id, :conversation_id, :agent_id, :status, :created_at, :updated_at)"
                ),
                {
                    "id": "0" * 32,
                    "conversation_id": "1" * 32,
                    "agent_id": "coordinator",
                    "status": "created",
                    "created_at": "2026-07-10T12:00:00Z",
                    "updated_at": "2026-07-10T12:00:00Z",
                },
            )
            raise RuntimeError("rollback")

    async with database.session() as session:
        count = await session.scalar(text("SELECT COUNT(*) FROM runs"))
    assert count == 0


def test_database_rejects_non_sqlite_or_sync_urls(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        Database("postgresql+asyncpg://localhost/agentcell")

    with pytest.raises(ConfigurationError):
        Database(f"sqlite:///{(tmp_path / 'sync.db').as_posix()}")
