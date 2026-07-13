"""Async SQLite engine and session lifecycle with mandatory safety PRAGMAs."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry

from agentcell.errors import ConfigurationError

SQLITE_BUSY_TIMEOUT_MS = 5_000


def sqlite_url(path: Path) -> str:
    """Build an absolute aiosqlite URL for a filesystem database."""

    resolved = path.expanduser().resolve()
    return URL.create("sqlite+aiosqlite", database=resolved.as_posix()).render_as_string(
        hide_password=False
    )


def ensure_sqlite_parent(url: str) -> None:
    """Create the parent directory for a file-backed SQLite URL."""

    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        raise ConfigurationError("AgentCell stage 2 supports SQLite database URLs only")
    if parsed.drivername != "sqlite+aiosqlite":
        raise ConfigurationError("SQLite URLs must use the aiosqlite driver")

    database = parsed.database
    if database and database != ":memory:":
        Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _set_sqlite_pragmas(
    dbapi_connection: DBAPIConnection,
    _connection_record: ConnectionPoolEntry,
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def configure_sqlite_engine(engine: Engine) -> None:
    """Attach mandatory SQLite PRAGMAs to every new DBAPI connection."""

    event.listen(engine, "connect", _set_sqlite_pragmas)


class Database:
    """Own an async engine and provide explicit session and transaction scopes."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        ensure_sqlite_parent(url)
        self._engine = create_async_engine(
            url,
            echo=echo,
            pool_pre_ping=True,
            connect_args={"timeout": SQLITE_BUSY_TIMEOUT_MS / 1_000},
        )
        configure_sqlite_engine(self._engine.sync_engine)
        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @classmethod
    def from_path(cls, path: Path, *, echo: bool = False) -> Database:
        """Create a Database for an absolute or relative SQLite file path."""

        return cls(sqlite_url(path), echo=echo)

    @property
    def engine(self) -> AsyncEngine:
        """Expose the engine for migrations, health checks, and controlled inspection."""

        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession]:
        """Yield a session without implicitly committing a transaction."""

        async with self._session_factory() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[AsyncSession]:
        """Yield a session whose transaction commits or rolls back atomically."""

        async with self._session_factory() as session, session.begin():
            yield session

    async def dispose(self) -> None:
        """Close pooled connections owned by this Database."""

        await self._engine.dispose()
