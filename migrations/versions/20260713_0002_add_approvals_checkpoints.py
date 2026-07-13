"""Add approvals, checkpoints, and durable tool execution ledger.

Revision ID: 20260713_0002
Revises: 20260710_0001
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260713_0002"
down_revision: str | None = "20260710_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("provider_call_id", sa.String(length=255), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("decided_at", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="ck_approvals_status"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_approvals_run_id_runs", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
        sa.UniqueConstraint("run_id", "provider_call_id", name="uq_approvals_run_provider_call"),
    )
    op.create_index("ix_approvals_run_status", "approvals", ["run_id", "status"])

    op.create_table(
        "checkpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.CheckConstraint("event_sequence >= 1", name="ck_checkpoints_sequence"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_checkpoints_run_id_runs", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_checkpoints"),
        sa.UniqueConstraint("run_id", "event_sequence", name="uq_checkpoints_run_sequence"),
    )
    op.create_index("ix_checkpoints_run_created", "checkpoints", ["run_id", "created_at"])

    op.create_table(
        "tool_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("provider_call_id", sa.String(length=255), nullable=False),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column("call_id", sa.Uuid(), nullable=False),
        sa.Column("idempotent", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.String(length=32), nullable=False),
        sa.Column("completed_at", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "status IN ('started', 'completed', 'failed')",
            name="ck_tool_executions_status",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name="fk_tool_executions_run_id_runs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tool_executions"),
        sa.UniqueConstraint(
            "run_id", "provider_call_id", name="uq_tool_executions_run_provider_call"
        ),
    )
    op.create_index("ix_tool_executions_run_status", "tool_executions", ["run_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_tool_executions_run_status", table_name="tool_executions")
    op.drop_table("tool_executions")
    op.drop_index("ix_checkpoints_run_created", table_name="checkpoints")
    op.drop_table("checkpoints")
    op.drop_index("ix_approvals_run_status", table_name="approvals")
    op.drop_table("approvals")
