"""Add nullable ``name`` column to ``sessions``.

Revision ID: 0004_add_sessions_name
Revises: 0003_add_sessions_claude_session_id
Create Date: 2026-05-06

CCR-037: human-readable session label, auto-filled from the first user
prompt (truncated to ~40 chars) and overwritable via ``/rename``. Existing
rows stay ``NULL``; the ``/sessions`` listing renders a stable
``(unnamed)`` placeholder for those. Reversible — :func:`downgrade` drops
the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_add_sessions_name"
down_revision: str | None = "0003_add_sessions_claude_session_id"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(sa.Column("name", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("name")
