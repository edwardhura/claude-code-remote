"""Initial schema: paired_users, pairing_codes, sessions.

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "paired_users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_username", sa.Text(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column(
            "is_owner",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("approved_by_tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_chat_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tg_user_id"),
    )
    op.create_index(
        "ix_paired_users_single_owner",
        "paired_users",
        ["is_owner"],
        unique=True,
        sqlite_where=sa.text("is_owner = 1"),
    )

    op.create_table(
        "pairing_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=8), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_username", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index(
        "ix_pairing_codes_tg_user_id_used_at",
        "pairing_codes",
        ["tg_user_id", "used_at"],
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_by_tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("first_prompt", sa.String(length=500), nullable=True),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sessions_status", "sessions", ["status"])
    op.create_index(
        "ix_sessions_started_at_desc",
        "sessions",
        [sa.text("started_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_started_at_desc", table_name="sessions")
    op.drop_index("ix_sessions_status", table_name="sessions")
    op.drop_table("sessions")

    op.drop_index("ix_pairing_codes_tg_user_id_used_at", table_name="pairing_codes")
    op.drop_table("pairing_codes")

    op.drop_index("ix_paired_users_single_owner", table_name="paired_users")
    op.drop_table("paired_users")
