"""Create Run projection and append-only event storage.

Revision ID: 20260710_0001
Revises: None
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260710_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the minimal Stage 2 schema and append-only guards."""

    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("parent_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.String(length=32), nullable=False),
        sa.Column(
            "next_event_sequence",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('created', 'running', 'waiting_approval', 'paused', "
            "'completed', 'failed', 'cancelled')",
            name="ck_runs_status",
        ),
        sa.CheckConstraint(
            "next_event_sequence >= 1",
            name="ck_runs_next_event_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"],
            ["runs.id"],
            name="fk_runs_parent_run_id_runs",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
    )
    op.create_index("ix_runs_conversation_id", "runs", ["conversation_id"], unique=False)
    op.create_index("ix_runs_parent_run_id", "runs", ["parent_run_id"], unique=False)

    op.create_table(
        "run_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.String(length=32), nullable=False),
        sa.CheckConstraint("payload_version >= 1", name="ck_run_events_payload_version"),
        sa.CheckConstraint("sequence >= 1", name="ck_run_events_sequence"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name="fk_run_events_run_id_runs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_events"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
    )
    op.create_index(
        "ix_run_events_run_occurred",
        "run_events",
        ["run_id", "occurred_at"],
        unique=False,
    )

    op.execute(
        """
        CREATE TRIGGER trg_run_events_no_update
        BEFORE UPDATE ON run_events
        BEGIN
            SELECT RAISE(ABORT, 'run_events is append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_run_events_no_delete
        BEFORE DELETE ON run_events
        BEGIN
            SELECT RAISE(ABORT, 'run_events is append-only');
        END
        """
    )


def downgrade() -> None:
    """Remove append-only guards and the Stage 2 schema."""

    op.execute("DROP TRIGGER IF EXISTS trg_run_events_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_run_events_no_update")
    op.drop_index("ix_run_events_run_occurred", table_name="run_events")
    op.drop_table("run_events")
    op.drop_index("ix_runs_parent_run_id", table_name="runs")
    op.drop_index("ix_runs_conversation_id", table_name="runs")
    op.drop_table("runs")
