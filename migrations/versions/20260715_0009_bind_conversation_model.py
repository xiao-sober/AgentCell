"""bind conversations to their configured model reference

Revision ID: 20260715_0009
Revises: 20260715_0008
Create Date: 2026-07-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260715_0009"
down_revision: str | None = "20260715_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("model_ref", sa.String(length=255), nullable=True))

    # Prefer a model that completed a real turn, then fall back to the earliest
    # identity-bearing Run. JSON is read directly so legacy v1 hash ordering does
    # not prevent recovery of the immutable model reference.
    op.execute(
        sa.text(
            """
            UPDATE conversations
            SET model_ref = (
                SELECT json_extract(r.execution_identity, '$.model_ref')
                FROM runs AS r
                WHERE r.conversation_id = conversations.id
                  AND r.execution_identity IS NOT NULL
                  AND json_extract(r.execution_identity, '$.model_ref') IS NOT NULL
                ORDER BY CASE WHEN r.status = 'completed' THEN 0 ELSE 1 END,
                         r.created_at,
                         r.id
                LIMIT 1
            )
            WHERE model_ref IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE conversations
            SET model_ref = (
                SELECT json_extract(a.data, '$.model_ref')
                FROM agents AS a
                WHERE a.id = conversations.agent_id
                LIMIT 1
            )
            WHERE model_ref IS NULL
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("model_ref")
