"""Drop UNIQUE flag on the ``ix_sessions_claude_session_id_not_null`` index.

Revision ID: 0005_drop_sessions_claude_session_id_unique
Revises: 0004_add_sessions_name
Create Date: 2026-05-06

CCR-041: ``--resume`` chains semantically produce multiple :class:`Session`
rows that point at the same Claude conversation, so the DB-wide uniqueness
constraint added by 0003 is incorrect for the going-forward shape. The
double-import guard for the ``session save`` path moves to a programmatic
SELECT-before-INSERT in :mod:`ccr.claude.import_session`. The partial
predicate (``claude_session_id IS NOT NULL``) stays so the index remains
useful for resume-time lookups by claude session id; only the unique flag
flips to False.

Reversible — :func:`downgrade` restores the unique flag.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_drop_sessions_claude_session_id_unique"
down_revision: str | None = "0004_add_sessions_name"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_index("ix_sessions_claude_session_id_not_null", table_name="sessions")
    op.create_index(
        "ix_sessions_claude_session_id_not_null",
        "sessions",
        ["claude_session_id"],
        unique=False,
        sqlite_where=sa.text("claude_session_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_claude_session_id_not_null", table_name="sessions")
    op.create_index(
        "ix_sessions_claude_session_id_not_null",
        "sessions",
        ["claude_session_id"],
        unique=True,
        sqlite_where=sa.text("claude_session_id IS NOT NULL"),
    )
