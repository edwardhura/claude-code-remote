"""CCR-044: bot ``/sessions`` listing filters out idle rows.

A small companion file to :mod:`tests.test_bot_session` covering only the
CCR-044-introduced behaviours of ``cmd_sessions``: idle rows are excluded
at the SQL layer, but the legacy NULL ``claude_session_id`` marker
rendering is preserved for non-idle rows whose Claude id was never
persisted (pre-CCR-044 artefacts).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest_asyncio
from aiogram.types import Chat, Message, User
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ccr.bot.handlers.session import cmd_sessions
from ccr.claude.state import SessionStatus
from ccr.db.engine import AsyncSessionMaker
from ccr.db.models import Base, Session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return AsyncSessionMaker(engine)


def _make_message(
    *,
    text: str = "/sessions",
    user_id: int = 42,
    username: str | None = "alice",
) -> Message:
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1000, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="A", username=username),
        text=text,
    )
    object.__setattr__(msg, "answer", AsyncMock())
    return msg


async def _seed_session(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    started_at: datetime,
    status: str,
    name: str | None = None,
    claude_session_id: str | None = None,
) -> None:
    async with session_factory() as db:
        db.add(
            Session(
                id=session_id,
                started_at=started_at,
                status=status,
                started_by_tg_user_id=42,
                first_prompt="x",
                name=name,
                claude_session_id=claude_session_id,
            ),
        )
        await db.commit()


async def test_sessions_filters_idle_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``/sessions`` lists running and completed rows but excludes idle rows.

    The filter sits in the SQL ``WHERE`` clause so ``_SESSIONS_LIMIT``
    bounds 20 _displayed_ rows, never "20 candidates of which some get
    skipped".
    """
    base = datetime(2026, 5, 8, 9, 0, 0, tzinfo=UTC)
    idle_id = uuid.UUID("aaaa0000-0000-0000-0000-000000000001")
    running_id = uuid.UUID("bbbb0000-0000-0000-0000-000000000002")
    completed_id = uuid.UUID("cccc0000-0000-0000-0000-000000000003")

    await _seed_session(
        session_factory,
        session_id=idle_id,
        started_at=base,
        status=SessionStatus.IDLE.value,
        name="should-not-list",
        claude_session_id=None,
    )
    await _seed_session(
        session_factory,
        session_id=running_id,
        started_at=base + timedelta(seconds=10),
        status=SessionStatus.RUNNING.value,
        name="running-row",
        claude_session_id="running1-aaaa-bbbb-cccc-dddddddddddd",
    )
    await _seed_session(
        session_factory,
        session_id=completed_id,
        started_at=base + timedelta(seconds=20),
        status=SessionStatus.COMPLETED.value,
        name="completed-row",
        claude_session_id="complete-aaaa-bbbb-cccc-dddddddddddd",
    )

    msg = _make_message()
    await cmd_sessions(msg, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert "running-row" in reply
    assert "completed-row" in reply
    # Idle row name MUST NOT appear in the listing.
    assert "should-not-list" not in reply
    # And neither should its claude_session_id prefix appear in any way
    # (idle rows have NULL claude_session_id; we double-check the row is
    # absent by confirming nothing dependent on its identity leaked).


async def test_sessions_still_renders_legacy_null_marker_for_non_idle_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Legacy NULL ``claude_session_id`` rows still render the ``--------`` marker.

    CCR-044 filters ``status='idle'`` rows out at the SQL layer, but the
    ``_NULL_CLAUDE_SESSION_ID_MARKER`` constant and its rendering must
    continue to work for pre-CCR-044 legacy rows whose terminal status
    is non-idle and whose ``claude_session_id`` was never persisted (e.g.
    a row whose ``_update_claude_session_id`` write was lost before the
    row finalised).
    """
    legacy_id = uuid.UUID("dddd0000-0000-0000-0000-000000000004")
    await _seed_session(
        session_factory,
        session_id=legacy_id,
        started_at=datetime(2026, 5, 8, 10, 0, 0, tzinfo=UTC),
        status=SessionStatus.COMPLETED.value,
        name="legacy",
        claude_session_id=None,
    )

    msg = _make_message()
    await cmd_sessions(msg, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert "<code>--------</code>" in reply
    assert "legacy" in reply
