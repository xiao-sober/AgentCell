"""Add file-backed Artifact metadata and FTS5 memory storage.

Revision ID: 20260713_0003
Revises: 20260713_0002
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260713_0003"
down_revision: str | None = "20260713_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("suggested_name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifacts_size"),
        sa.PrimaryKeyConstraint("id", name="pk_artifacts"),
        sa.UniqueConstraint("sha256", "size_bytes", name="uq_artifacts_hash_size"),
        sa.UniqueConstraint("storage_key", name="uq_artifacts_storage_key"),
    )
    op.create_index("ix_artifacts_created_at", "artifacts", ["created_at"])

    op.create_table(
        "memory_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.String(length=512), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("normalized_hash", sa.String(length=64), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False),
        sa.Column("sensitive", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "kind IN ('working', 'conversation', 'episodic', 'semantic')",
            name="ck_memory_items_kind",
        ),
        sa.CheckConstraint(
            "importance >= 0 AND importance <= 1",
            name="ck_memory_importance",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memory_items"),
    )
    op.create_index(
        "ix_memory_scope",
        "memory_items",
        ["user_id", "project_id", "agent_id"],
    )
    op.create_index("ix_memory_expiry", "memory_items", ["expires_at"])
    op.create_index("ix_memory_hash", "memory_items", ["normalized_hash"])

    op.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "memory_id UNINDEXED, content, tags, tokenize='unicode61')"
    )
    op.execute(
        "CREATE TRIGGER trg_memory_items_fts_insert AFTER INSERT ON memory_items BEGIN "
        "INSERT INTO memory_fts(memory_id, content, tags) "
        "VALUES (new.id, new.content, new.tags); END"
    )
    op.execute(
        "CREATE TRIGGER trg_memory_items_fts_update AFTER UPDATE ON memory_items BEGIN "
        "DELETE FROM memory_fts WHERE memory_id = old.id; "
        "INSERT INTO memory_fts(memory_id, content, tags) "
        "VALUES (new.id, new.content, new.tags); END"
    )
    op.execute(
        "CREATE TRIGGER trg_memory_items_fts_delete AFTER DELETE ON memory_items BEGIN "
        "DELETE FROM memory_fts WHERE memory_id = old.id; END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_memory_items_fts_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_items_fts_update")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_items_fts_insert")
    op.execute("DROP TABLE IF EXISTS memory_fts")
    op.drop_index("ix_memory_hash", table_name="memory_items")
    op.drop_index("ix_memory_expiry", table_name="memory_items")
    op.drop_index("ix_memory_scope", table_name="memory_items")
    op.drop_table("memory_items")
    op.drop_index("ix_artifacts_created_at", table_name="artifacts")
    op.drop_table("artifacts")
