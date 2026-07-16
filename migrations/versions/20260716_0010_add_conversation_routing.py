"""add fixed and auto Conversation routing bindings

Revision ID: 20260716_0010
Revises: 20260715_0009
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260716_0010"
down_revision: str | None = "20260715_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH_TEMP_TABLE = "_alembic_tmp_conversations"


def _drop_batch_residue() -> None:
    """Remove a batch copy only while the authoritative source table still exists."""

    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if _BATCH_TEMP_TABLE not in tables:
        return
    if "conversations" not in tables:
        raise RuntimeError(
            "Interrupted conversations migration requires manual recovery: "
            "the Alembic batch table exists but the source table is missing"
        )
    op.drop_table(_BATCH_TEMP_TABLE)


def upgrade() -> None:
    _drop_batch_residue()
    op.add_column(
        "conversations",
        sa.Column(
            "routing_mode",
            sa.String(length=16),
            nullable=False,
            server_default="fixed",
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("team_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("routing_policy_version", sa.String(length=64), nullable=True),
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER ck_conversations_routing_mode_insert "
            "BEFORE INSERT ON conversations "
            "WHEN NEW.routing_mode NOT IN ('fixed', 'auto') "
            "BEGIN SELECT RAISE(ABORT, 'invalid conversations.routing_mode'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER ck_conversations_routing_mode_update "
            "BEFORE UPDATE OF routing_mode ON conversations "
            "WHEN NEW.routing_mode NOT IN ('fixed', 'auto') "
            "BEGIN SELECT RAISE(ABORT, 'invalid conversations.routing_mode'); END"
        )
    )


def downgrade() -> None:
    _drop_batch_residue()
    op.execute(sa.text("DROP TRIGGER IF EXISTS ck_conversations_routing_mode_update"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS ck_conversations_routing_mode_insert"))
    op.drop_column("conversations", "routing_policy_version")
    op.drop_column("conversations", "team_id")
    op.drop_column("conversations", "routing_mode")
