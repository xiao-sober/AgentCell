"""Alembic environment for AgentCell's async SQLite database."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from agentcell.storage.database import configure_sqlite_engine, ensure_sqlite_parent
from agentcell.storage.tables import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_FTS5_TABLES = {
    "memory_fts",
    "memory_fts_config",
    "memory_fts_content",
    "memory_fts_data",
    "memory_fts_docsize",
    "memory_fts_idx",
}


def _include_object(
    object_: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object,
) -> bool:
    """Ignore only the FTS5 virtual table and its SQLite-managed shadow tables."""

    del object_, compare_to
    return not (type_ == "table" and reflected and name in _FTS5_TABLES)


def _database_url() -> str:
    url = os.getenv("AGENTCELL_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("Alembic requires sqlalchemy.url or AGENTCELL_DATABASE_URL")
    ensure_sqlite_parent(url)
    return url


def run_migrations_offline() -> None:
    """Generate SQL without opening a database connection."""

    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
        include_object=_include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
        include_object=_include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    configure_sqlite_engine(connectable.sync_engine)

    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Apply migrations through an async SQLAlchemy engine."""

    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
