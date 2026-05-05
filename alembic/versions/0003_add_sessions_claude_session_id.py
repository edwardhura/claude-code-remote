"""Add nullable ``claude_session_id`` column + partial unique index to ``sessions``.

Revision ID: 0003_add_sessions_claude_session_id
Revises: 0002_add_paired_users_timezone
Create Date: 2026-05-05

CCR-036: persist Claude's own ``session_id`` on each :class:`~ccr.db.models.Session`
row so ``/continue`` can resume via ``claude --resume <claude_session_id>``. The
partial unique index where ``claude_session_id IS NOT NULL`` is the
authoritative double-import guard for the new ``session save`` import path.
Reversible — :func:`downgrade` drops the index first, then the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003_add_sessions_claude_session_id"
down_revision: str | None = "0002_add_paired_users_timezone"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(sa.Column("claude_session_id", sa.Text(), nullable=True))
    op.create_index(
        "ix_sessions_claude_session_id_not_null",
        "sessions",
        ["claude_session_id"],
        unique=True,
        sqlite_where=sa.text("claude_session_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_claude_session_id_not_null", table_name="sessions")
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("claude_session_id")
