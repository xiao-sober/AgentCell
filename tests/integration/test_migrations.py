from __future__ import annotations

import json
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
        delegation_indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list('agent_delegations')").fetchall()
        }
        message_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list('messages')").fetchall()
        }
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info('runs')").fetchall()}
        conversation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('conversations')").fetchall()
        }
    finally:
        connection.close()

    assert {
        "alembic_version",
        "runs",
        "run_events",
        "approvals",
        "checkpoints",
        "tool_executions",
        "artifacts",
        "memory_items",
        "memory_fts",
        "agent_delegations",
        "agents",
        "conversations",
        "messages",
        "change_sets",
        "file_changes",
    } <= tables
    assert {
        "trg_run_events_no_update",
        "trg_run_events_no_delete",
        "trg_memory_items_fts_insert",
        "trg_memory_items_fts_update",
        "trg_memory_items_fts_delete",
        "ck_conversations_routing_mode_insert",
        "ck_conversations_routing_mode_update",
    } <= triggers
    assert {"ix_runs_conversation_id", "ix_runs_parent_run_id"} <= run_indexes
    assert "ix_run_events_run_occurred" in event_indexes
    assert "ix_agent_delegations_parent_status" in delegation_indexes
    assert "ix_messages_conversation_run" in message_indexes
    assert "execution_identity" in run_columns
    assert {
        "model_ref",
        "routing_mode",
        "team_id",
        "routing_policy_version",
    } <= conversation_columns


def test_conversation_model_binding_migration_backfills_completed_run_identity(
    database_path: Path,
    migrated_alembic_config: Config,
) -> None:
    command.downgrade(migrated_alembic_config, "20260715_0008")
    conversation_id = "1" * 32
    run_id = "2" * 32
    now = "2026-07-15T00:00:00Z"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO conversations "
            "(id, user_id, project_id, workspace, agent_id, title, active_run_id, "
            "next_message_sequence, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, 1, ?, ?)",
            (conversation_id, "3" * 32, "project", "G:\\workspace", "coder", now, now),
        )
        connection.execute(
            "INSERT INTO runs "
            "(id, conversation_id, agent_id, execution_identity, parent_run_id, status, "
            "created_at, updated_at, next_event_sequence) "
            "VALUES (?, ?, ?, ?, NULL, 'completed', ?, ?, 1)",
            (
                run_id,
                conversation_id,
                "coder",
                json.dumps({"model_ref": "deepseek_pro"}),
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    command.upgrade(migrated_alembic_config, "head")
    connection = sqlite3.connect(database_path)
    try:
        model_ref = connection.execute(
            "SELECT model_ref FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
    finally:
        connection.close()

    assert model_ref == ("deepseek_pro",)


def test_conversation_routing_migration_recovers_sqlite_batch_copy_residue(
    database_path: Path,
    migrated_alembic_config: Config,
) -> None:
    command.downgrade(migrated_alembic_config, "20260715_0009")
    connection = sqlite3.connect(database_path)
    try:
        now = "2026-07-16T00:00:00Z"
        conversation_id = "4" * 32
        connection.execute(
            "INSERT INTO conversations "
            "(id, user_id, project_id, workspace, agent_id, title, active_run_id, "
            "next_message_sequence, created_at, updated_at, model_ref) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, 1, ?, ?, ?)",
            (
                conversation_id,
                "5" * 32,
                "project",
                "G:\\workspace",
                "coordinator",
                now,
                now,
                "qwen_plus",
            ),
        )
        connection.execute("CREATE TABLE _alembic_tmp_conversations (id CHAR(32) PRIMARY KEY)")
        connection.execute(
            "INSERT INTO _alembic_tmp_conversations (id) VALUES (?)",
            (conversation_id,),
        )
        connection.commit()
    finally:
        connection.close()

    command.upgrade(migrated_alembic_config, "head")

    connection = sqlite3.connect(database_path)
    try:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        residue = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = '_alembic_tmp_conversations'"
        ).fetchone()
        columns = {row[1] for row in connection.execute("PRAGMA table_info('conversations')")}
        preserved = connection.execute(
            "SELECT routing_mode, model_ref FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
    finally:
        connection.close()

    assert version == ("20260716_0010",)
    assert residue is None
    assert {"routing_mode", "team_id", "routing_policy_version"} <= columns
    assert preserved == ("fixed", "qwen_plus")


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
    assert "approvals" not in tables_after_downgrade
    assert "checkpoints" not in tables_after_downgrade
    assert "tool_executions" not in tables_after_downgrade
    assert "artifacts" not in tables_after_downgrade
    assert "memory_items" not in tables_after_downgrade
    assert "memory_fts" not in tables_after_downgrade
    assert "agent_delegations" not in tables_after_downgrade
    assert "agents" not in tables_after_downgrade
    assert "conversations" not in tables_after_downgrade
    assert "messages" not in tables_after_downgrade
    assert "change_sets" not in tables_after_downgrade
    assert "file_changes" not in tables_after_downgrade

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
    assert {
        "runs",
        "run_events",
        "approvals",
        "checkpoints",
        "tool_executions",
        "artifacts",
        "memory_items",
        "memory_fts",
        "agent_delegations",
        "agents",
        "conversations",
        "messages",
        "change_sets",
        "file_changes",
    } <= tables_after_upgrade


def test_migration_metadata_has_no_drift(migrated_alembic_config: Config) -> None:
    command.check(migrated_alembic_config)
