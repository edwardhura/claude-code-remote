"""Add nullable ``timezone`` column to ``paired_users``.

Revision ID: 0002_add_paired_users_timezone
Revises: 0001_initial
Create Date: 2026-05-05

CCR-034: per-user timezone preference. Existing rows stay ``NULL``; the
render-time helper (CCR-035) will treat ``NULL`` as UTC. Reversible —
``downgrade`` drops the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_paired_users_timezone"
down_revision: str | None = "0001_initial"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("paired_users") as batch_op:
        batch_op.add_column(sa.Column("timezone", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("paired_users") as batch_op:
        batch_op.drop_column("timezone")
