"""SQLAlchemy 2.x ORM models for the three persistent SQLite tables.

The schema mirrors plan §5 verbatim:

* ``paired_users`` — approved Telegram user IDs; partial unique index ensures
  at most one row has ``is_owner = true``.
* ``pairing_codes`` — short-lived pairing offers awaiting console approval.
* ``sessions`` — Claude Code session metadata; events live as JSONL on disk.
"""

from __future__ import annotations

import uuid
from datetime import datetime  # noqa: TC003 — Mapped[datetime] needs runtime symbol

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base shared by every persistent model."""


class PairedUser(Base):
    """An approved Telegram user permitted to drive the bot.

    `revoked_at IS NULL` defines an active allowlist entry. There are no hard
    deletes — revocation is a soft flag preserved for audit. The first
    approval auto-promotes to owner; the partial unique index enforces the
    "exactly one owner" invariant at the database level.
    """

    __tablename__ = "paired_users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    tg_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_by_tg_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        Index(
            "ix_paired_users_single_owner",
            "is_owner",
            unique=True,
            sqlite_where=(is_owner.is_(True)),
        ),
    )


class PairingCode(Base):
    """An 8-char pairing offer awaiting console approval.

    Approval inserts/updates the corresponding `paired_users` row and stamps
    `used_at` on this code, atomically.
    """

    __tablename__ = "pairing_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    code: Mapped[str] = mapped_column(String(8), unique=True, nullable=False)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tg_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_pairing_codes_tg_user_id_used_at", "tg_user_id", "used_at"),)


class Session(Base):
    """Claude Code session metadata.

    Events are NOT stored here — they live as JSONL under
    ``data/logs/<session_id>.jsonl``. ``last_event_at`` powers viewer "stuck"
    detection; ``status`` is a string enum {`running`, `completed`,
    `stopped`, `crashed`}.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_by_tg_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    first_prompt: Mapped[str | None] = mapped_column(String(500), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_sessions_status", "status"),
        Index("ix_sessions_started_at_desc", started_at.desc()),
    )


__all__ = ["Base", "PairedUser", "PairingCode", "Session"]
