"""add durable Agent delegations

Revision ID: 20260713_0004
Revises: 20260713_0003
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260713_0004"
down_revision: str | None = "20260713_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_delegations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("parent_run_id", sa.Uuid(), nullable=False),
        sa.Column("child_run_id", sa.Uuid(), nullable=False),
        sa.Column("provider_call_id", sa.String(length=255), nullable=False),
        sa.Column("target_agent_id", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.String(length=32), nullable=False),
        sa.CheckConstraint("depth >= 1", name="ck_agent_delegations_depth"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'waiting_approval', 'completed', "
            "'failed', 'cancelled')",
            name="ck_agent_delegations_status",
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"],
            ["runs.id"],
            name="fk_agent_delegations_parent_run_id_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["child_run_id"],
            ["runs.id"],
            name="fk_agent_delegations_child_run_id_runs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_delegations"),
        sa.UniqueConstraint(
            "parent_run_id",
            "provider_call_id",
            name="uq_agent_delegations_parent_call",
        ),
        sa.UniqueConstraint("child_run_id", name="uq_agent_delegations_child_run"),
    )
    op.create_index(
        "ix_agent_delegations_parent_status",
        "agent_delegations",
        ["parent_run_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_delegations_parent_status", table_name="agent_delegations")
    op.drop_table("agent_delegations")
