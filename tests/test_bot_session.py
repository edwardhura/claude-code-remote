"""Tests for the session lifecycle handlers.

We call the handlers directly (matching the pattern in
``test_bot_pairing.py``) and pass a stub :class:`SessionManager` plus a
real in-memory DB factory via aiogram workflow data conventions. This
exercises the dispatch logic without spinning up a real Telegram bot.
"""

from __future__ import annotations

import re
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

from ccr.auth.pairing import approve, create_code
from ccr.bot.handlers.session import (
    cmd_clear,
    cmd_new,
    cmd_pid,
    cmd_sessions,
    cmd_stop,
    cmd_who,
    handle_text,
)
from ccr.claude.manager import NoActiveSessionError, SessionError
from ccr.claude.state import SessionStatus
from ccr.db.engine import AsyncSessionMaker
from ccr.db.models import Base, Session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine


# --------------------------------------------------------------------------- #
# Fixtures.
# --------------------------------------------------------------------------- #


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
    text: str,
    user_id: int = 42,
    username: str | None = "alice",
    chat_id: int = 1000,
) -> Message:
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="A", username=username),
        text=text,
    )
    object.__setattr__(msg, "answer", AsyncMock())
    return msg


_DEFAULT_SESSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


class FakeManager:
    """Stub :class:`SessionManager` exposing only the surface the handlers use."""

    def __init__(
        self,
        *,
        status: SessionStatus = SessionStatus.IDLE,
        pid: int | None = 4242,
        started_at: datetime | None = None,
        session_id: uuid.UUID = _DEFAULT_SESSION_ID,
    ) -> None:
        self._status = status
        self._pid = pid
        self._session_id = session_id
        self._started_at = started_at if started_at is not None else datetime.now(UTC)
        self.new_session = AsyncMock()
        self.send = AsyncMock()
        self.stop = AsyncMock()
        # Default new_session returns a stable UUID.
        self.new_session.return_value = self._session_id

    async def status(self) -> SessionStatus:
        return self._status

    async def info(self) -> dict[str, object]:
        if self._status == SessionStatus.IDLE:
            return {
                "session_id": None,
                "pid": None,
                "started_at": None,
                "status": SessionStatus.IDLE,
            }
        return {
            "session_id": self._session_id,
            "pid": self._pid,
            "started_at": self._started_at,
            "status": self._status,
        }

    def set_status(self, status: SessionStatus) -> None:
        self._status = status


# --------------------------------------------------------------------------- #
# /new
# --------------------------------------------------------------------------- #


async def test_cmd_new_starts_session_and_replies_with_short_id_and_pid(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = FakeManager(pid=4242)

    async def _fake_new_session(*, prompt: str | None, started_by_tg_user_id: int) -> uuid.UUID:
        del prompt, started_by_tg_user_id
        manager.set_status(SessionStatus.RUNNING)
        return _DEFAULT_SESSION_ID

    manager.new_session.side_effect = _fake_new_session
    msg = _make_message(text="/new", user_id=42)

    await cmd_new(msg, session_manager=manager, db_factory=session_factory)

    manager.new_session.assert_awaited_once_with(prompt=None, started_by_tg_user_id=42)
    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert reply == "Session 11111111 started (pid 4242)."
    assert re.search(r"\(pid \d+\)", reply) is not None


async def test_cmd_new_reports_session_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = FakeManager()
    manager.new_session.side_effect = SessionError("claude binary not found")
    msg = _make_message(text="/new")

    await cmd_new(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    assert "claude binary not found" in msg.answer.await_args.args[0]


# --------------------------------------------------------------------------- #
# /stop
# --------------------------------------------------------------------------- #


async def test_cmd_stop_on_idle_replies_no_active_session() -> None:
    manager = FakeManager(status=SessionStatus.IDLE)
    msg = _make_message(text="/stop")

    await cmd_stop(msg, session_manager=manager)

    manager.stop.assert_not_called()
    msg.answer.assert_awaited_once_with("No active session.")


async def test_cmd_stop_on_running_calls_stop_and_replies() -> None:
    manager = FakeManager(status=SessionStatus.RUNNING)
    msg = _make_message(text="/stop")

    await cmd_stop(msg, session_manager=manager)

    manager.stop.assert_awaited_once_with()
    msg.answer.assert_awaited_once_with("Session stopped.")


# --------------------------------------------------------------------------- #
# /clear
# --------------------------------------------------------------------------- #


async def test_cmd_clear_stops_then_starts_fresh(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = FakeManager(status=SessionStatus.RUNNING, pid=9999)
    msg = _make_message(text="/clear", user_id=7)

    await cmd_clear(msg, session_manager=manager, db_factory=session_factory)

    manager.stop.assert_awaited_once_with()
    manager.new_session.assert_awaited_once_with(prompt=None, started_by_tg_user_id=7)
    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert "started" in reply
    assert re.search(r"\(pid \d+\)", reply) is not None


# --------------------------------------------------------------------------- #
# /pid
# --------------------------------------------------------------------------- #


async def test_cmd_pid_idle_returns_no_active_session() -> None:
    manager = FakeManager(status=SessionStatus.IDLE)
    msg = _make_message(text="/pid")

    await cmd_pid(msg, session_manager=manager)

    msg.answer.assert_awaited_once_with("No active session.")


async def test_cmd_pid_active_returns_session_pid_and_uptime() -> None:
    started_at = datetime.now(UTC)
    manager = FakeManager(
        status=SessionStatus.RUNNING,
        pid=12345,
        started_at=started_at,
        session_id=uuid.UUID("abcdef01-2345-6789-abcd-ef0123456789"),
    )
    msg = _make_message(text="/pid")

    await cmd_pid(msg, session_manager=manager)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert re.match(r"^Session [0-9a-f]{8} · pid \d+ · running \d", reply) is not None
    assert "abcdef01" in reply
    assert "12345" in reply


async def test_cmd_pid_active_uptime_minutes_format() -> None:
    started_at = datetime.now(UTC) - timedelta(minutes=2, seconds=10)
    manager = FakeManager(status=SessionStatus.RUNNING, pid=777, started_at=started_at)
    msg = _make_message(text="/pid")

    await cmd_pid(msg, session_manager=manager)

    reply = msg.answer.await_args.args[0]
    assert re.search(r"running \d+m \d+s$", reply) is not None


# --------------------------------------------------------------------------- #
# /who
# --------------------------------------------------------------------------- #


async def test_cmd_who_owner_sees_full_table(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Seed an owner via the real pairing flow.
    async with session_factory() as db:
        pc = await create_code(db, tg_user_id=111, tg_username="ownerx")
        await db.commit()
    async with session_factory() as db:
        await approve(db, pc.code)

    msg = _make_message(text="/who", user_id=111, username="ownerx")
    await cmd_who(msg, db_factory=session_factory, is_paired_user=True)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert "Paired users" in reply
    assert "111" in reply
    assert "@ownerx" in reply


async def test_cmd_who_friend_sees_count_and_owner_handle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        pc1 = await create_code(db, tg_user_id=111, tg_username="ownerx")
        await db.commit()
    async with session_factory() as db:
        await approve(db, pc1.code)
    async with session_factory() as db:
        pc2 = await create_code(db, tg_user_id=222, tg_username="bob")
        await db.commit()
    async with session_factory() as db:
        await approve(db, pc2.code)

    msg = _make_message(text="/who", user_id=222, username="bob")
    await cmd_who(msg, db_factory=session_factory, is_paired_user=True)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert "Paired users:" in reply
    assert "@ownerx" in reply
    # Friend reply must NOT include the table header.
    assert "<b>Paired users</b>" not in reply


# --------------------------------------------------------------------------- #
# Plain text handler
# --------------------------------------------------------------------------- #


async def test_plain_text_idle_starts_session_with_prompt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = FakeManager(status=SessionStatus.IDLE)
    msg = _make_message(text="please list files", user_id=99)

    await handle_text(msg, session_manager=manager, db_factory=session_factory)

    manager.new_session.assert_awaited_once_with(
        prompt="please list files",
        started_by_tg_user_id=99,
    )
    manager.send.assert_not_called()
    msg.answer.assert_awaited_once_with("Forwarded.")


async def test_plain_text_running_forwards_via_send(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = FakeManager(status=SessionStatus.RUNNING)
    msg = _make_message(text="follow up", user_id=99)

    await handle_text(msg, session_manager=manager, db_factory=session_factory)

    manager.send.assert_awaited_once_with("follow up")
    manager.new_session.assert_not_called()
    msg.answer.assert_awaited_once_with("Forwarded.")


async def test_plain_text_no_active_session_race_replies_cleanly(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """If `manager.send` races with stop and raises NoActiveSessionError, reply gracefully."""
    manager = FakeManager(status=SessionStatus.RUNNING)
    manager.send.side_effect = NoActiveSessionError("No active session.")
    msg = _make_message(text="hello")

    await handle_text(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once_with("No active session.")


async def test_plain_text_session_error_replies_with_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = FakeManager(status=SessionStatus.IDLE)
    manager.new_session.side_effect = SessionError("claude binary not found")
    msg = _make_message(text="hi")

    await handle_text(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    assert "claude binary not found" in msg.answer.await_args.args[0]


# --------------------------------------------------------------------------- #
# /sessions
# --------------------------------------------------------------------------- #


async def _seed_session(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    started_at: datetime,
    status: str = "completed",
    started_by_tg_user_id: int | None = 42,
    first_prompt: str | None = "hello world",
) -> None:
    async with session_factory() as db:
        db.add(
            Session(
                id=session_id,
                started_at=started_at,
                status=status,
                started_by_tg_user_id=started_by_tg_user_id,
                first_prompt=first_prompt,
            ),
        )
        await db.commit()


async def test_cmd_sessions_empty_db_returns_stable_string(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    msg = _make_message(text="/sessions")

    await cmd_sessions(msg, db_factory=session_factory)

    msg.answer.assert_awaited_once_with("(no sessions)")


async def test_cmd_sessions_three_rows_returns_three_lines_in_desc_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)
    sid_oldest = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    sid_middle = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
    sid_newest = uuid.UUID("cccccccc-0000-0000-0000-000000000003")
    await _seed_session(
        session_factory,
        session_id=sid_oldest,
        started_at=base,
        status="completed",
        first_prompt="oldest",
    )
    await _seed_session(
        session_factory,
        session_id=sid_middle,
        started_at=base + timedelta(seconds=10),
        status="stopped",
        first_prompt="middle",
    )
    await _seed_session(
        session_factory,
        session_id=sid_newest,
        started_at=base + timedelta(seconds=20),
        status="crashed",
        first_prompt="newest",
    )

    msg = _make_message(text="/sessions")
    await cmd_sessions(msg, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert "aaaaaaaa" in reply
    assert "bbbbbbbb" in reply
    assert "cccccccc" in reply
    pos_newest = reply.index("cccccccc")
    pos_middle = reply.index("bbbbbbbb")
    pos_oldest = reply.index("aaaaaaaa")
    assert pos_newest < pos_middle < pos_oldest


async def test_cmd_sessions_long_prompt_truncated_to_60_chars(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sid = uuid.UUID("dddddddd-0000-0000-0000-000000000004")
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=datetime(2026, 4, 30, 13, 0, 0, tzinfo=UTC),
        status="completed",
        first_prompt="x" * 80,
    )

    msg = _make_message(text="/sessions")
    await cmd_sessions(msg, db_factory=session_factory)

    reply = msg.answer.await_args.args[0]
    assert "x" * 60 in reply
    assert "x" * 61 not in reply


async def test_cmd_sessions_status_field_passed_through_verbatim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = datetime(2026, 4, 30, 14, 0, 0, tzinfo=UTC)
    await _seed_session(
        session_factory,
        session_id=uuid.UUID("eeeeeeee-0000-0000-0000-000000000005"),
        started_at=base,
        status="crashed",
        first_prompt="boom",
    )
    await _seed_session(
        session_factory,
        session_id=uuid.UUID("ffffffff-0000-0000-0000-000000000006"),
        started_at=base + timedelta(seconds=5),
        status="completed",
        first_prompt="ok",
    )

    msg = _make_message(text="/sessions")
    await cmd_sessions(msg, db_factory=session_factory)

    reply = msg.answer.await_args.args[0]
    assert "crashed" in reply
    assert "completed" in reply
