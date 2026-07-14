"""add durable change sets and file changes

Revision ID: 20260714_0007
Revises: 20260714_0006
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260714_0007"
down_revision: str | None = "20260714_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "change_sets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("completed_at", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'conflict', 'reverted')",
            name="ck_change_sets_status",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_change_sets_run_id_runs", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_change_sets"),
        sa.UniqueConstraint("run_id", name="uq_change_sets_run"),
    )
    op.create_index("ix_change_sets_run_status", "change_sets", ["run_id", "status"])
    op.create_table(
        "file_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("change_set_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(length=2048), nullable=False),
        sa.Column("provider_call_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("completed_at", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "status IN ('prepared', 'applied', 'completed', 'conflict', 'failed', 'reverted')",
            name="ck_file_changes_status",
        ),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["change_sets.id"],
            name="fk_file_changes_change_set_id_change_sets",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_file_changes_run_id_runs", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_file_changes"),
    )
    op.create_index("ix_file_changes_run_created", "file_changes", ["run_id", "created_at"])
    op.create_index("ix_file_changes_set_created", "file_changes", ["change_set_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_file_changes_set_created", table_name="file_changes")
    op.drop_index("ix_file_changes_run_created", table_name="file_changes")
    op.drop_table("file_changes")
    op.drop_index("ix_change_sets_run_status", table_name="change_sets")
    op.drop_table("change_sets")
