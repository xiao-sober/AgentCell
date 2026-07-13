from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def test_initial_migration_creates_constraints_indexes_and_append_only_triggers(
    database_path: Path,
    migrated_database_url: str,
) -> None:
    del migrated_database_url
    connection = sqlite3.connect(database_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        run_indexes = {row[1] for row in connection.execute("PRAGMA index_list('runs')").fetchall()}
        event_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list('run_events')").fetchall()
        }
    finally:
        connection.close()

    assert {"alembic_version", "runs", "run_events"} <= tables
    assert triggers == {"trg_run_events_no_update", "trg_run_events_no_delete"}
    assert {"ix_runs_conversation_id", "ix_runs_parent_run_id"} <= run_indexes
    assert "ix_run_events_run_occurred" in event_indexes


def test_initial_migration_can_downgrade_and_upgrade_again(
    database_path: Path,
    migrated_alembic_config: Config,
) -> None:
    command.downgrade(migrated_alembic_config, "base")
    connection = sqlite3.connect(database_path)
    try:
        tables_after_downgrade = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "runs" not in tables_after_downgrade
    assert "run_events" not in tables_after_downgrade

    command.upgrade(migrated_alembic_config, "head")
    connection = sqlite3.connect(database_path)
    try:
        tables_after_upgrade = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert {"runs", "run_events"} <= tables_after_upgrade
