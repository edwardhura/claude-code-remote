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
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Chat, Message, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ccr.auth.pairing import approve, create_code
from ccr.bot.handlers.session import (
    _DIVIDER_MESSAGE,
    _RENAME_USAGE_HINT,
    cmd_new,
    cmd_pid,
    cmd_rename,
    cmd_sessions,
    cmd_stop,
    cmd_who,
    handle_text,
)
from ccr.claude.manager import NoActiveSessionError, SessionError
from ccr.claude.state import SessionStatus
from ccr.db.engine import AsyncSessionMaker
from ccr.db.models import Base, PairedUser, Session

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
    bot: object | None = None,
) -> Message:
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="A", username=username),
        text=text,
    )
    object.__setattr__(msg, "answer", AsyncMock())
    if bot is not None:
        # ``Message.bot`` is an aiogram ``@property`` over ``_bot`` — direct
        # ``object.__setattr__(msg, "bot", ...)`` raises (no setter). The
        # canonical aiogram way to attach a Bot to a fabricated message is the
        # ``as_(bot)`` helper, which sets ``_bot`` and returns ``self``.
        msg.as_(bot)  # type: ignore[arg-type]
    return msg


async def _seed_paired_user(
    factory: async_sessionmaker[AsyncSession],
    *,
    tg_user_id: int,
    last_chat_id: int | None,
    is_owner: bool = False,
) -> None:
    async with factory() as db:
        db.add(
            PairedUser(
                tg_user_id=tg_user_id,
                tg_username=f"u{tg_user_id}",
                is_owner=is_owner,
                approved_at=datetime.now(UTC),
                last_chat_id=last_chat_id,
            ),
        )
        await db.commit()


_DEFAULT_SESSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


class FakeManager:
    """Stub :class:`SessionManager` exposing only the surface the handlers use.

    The default ``subprocess_held`` mirrors the legacy "subprocess held iff
    status != IDLE" coupling — every existing call site gets the same
    behaviour as before. Pass ``subprocess_held=True`` together with
    ``status=SessionStatus.IDLE`` to model the CCR-044 ``[idle]`` window
    where ``new_session`` has acquired a subprocess but ``SystemInit`` has
    not yet fired (so :meth:`info` returns a non-``None`` ``session_id``
    while ``status`` is still ``IDLE``).
    """

    def __init__(
        self,
        *,
        status: SessionStatus = SessionStatus.IDLE,
        pid: int | None = 4242,
        started_at: datetime | None = None,
        session_id: uuid.UUID = _DEFAULT_SESSION_ID,
        subprocess_held: bool | None = None,
    ) -> None:
        self._status = status
        self._pid = pid
        self._session_id = session_id
        self._started_at = started_at if started_at is not None else datetime.now(UTC)
        self._subprocess_held = (
            subprocess_held if subprocess_held is not None else status != SessionStatus.IDLE
        )
        self.new_session = AsyncMock()
        self.send = AsyncMock()
        self.stop = AsyncMock()
        # Default new_session returns a stable UUID.
        self.new_session.return_value = self._session_id

    async def status(self) -> SessionStatus:
        return self._status

    async def info(self) -> dict[str, object]:
        if not self._subprocess_held:
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
        if status != SessionStatus.IDLE:
            self._subprocess_held = True


# --------------------------------------------------------------------------- #
# /new
# --------------------------------------------------------------------------- #


async def test_cmd_new_starts_session_and_replies_with_pid(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CCR-045: ``/new`` reply exposes pid only; the local-UUID prefix is dropped."""
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
    assert reply == "New session started (pid 4242)."
    # Local-UUID prefix MUST NOT appear in the reply.
    assert "11111111" not in reply
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


async def test_cmd_stop_on_idle_no_subprocess_replies_no_active_session() -> None:
    """No-arg ``/stop`` with no subprocess held → "No active session." reply.

    CCR-046: the discriminator for "is there a subprocess to stop" is
    :meth:`SessionManager.info`'s ``session_id`` field, not ``status()``.
    Here ``subprocess_held=False`` → ``info()['session_id'] is None`` →
    the no-active-session branch.
    """
    manager = FakeManager(status=SessionStatus.IDLE)
    msg = _make_message(text="/stop")

    await cmd_stop(msg, session_manager=manager)

    manager.stop.assert_not_called()
    msg.answer.assert_awaited_once_with("No active session.")


async def test_cmd_stop_in_idle_window_with_subprocess_held_stops_session() -> None:
    """CCR-046 + CCR-044: ``/stop`` works in the pre-``SystemInit`` ``[idle]`` window.

    A subprocess is held (``info()['session_id']`` is not ``None``) but the
    in-memory status is still ``IDLE`` because ``SystemInit`` has not fired
    yet. The handler must stop the subprocess — using ``status() == IDLE``
    as the no-active-session discriminator (the pre-CCR-046 logic) would
    incorrectly skip ``stop()`` and reply "No active session."
    """
    manager = FakeManager(
        status=SessionStatus.IDLE,
        subprocess_held=True,
    )
    msg = _make_message(text="/stop")

    await cmd_stop(msg, session_manager=manager)

    manager.stop.assert_awaited_once_with()
    msg.answer.assert_awaited_once_with("Session stopped.")


async def test_cmd_stop_on_running_calls_stop_and_replies() -> None:
    manager = FakeManager(status=SessionStatus.RUNNING)
    msg = _make_message(text="/stop")

    await cmd_stop(msg, session_manager=manager)

    manager.stop.assert_awaited_once_with()
    msg.answer.assert_awaited_once_with("Session stopped.")


# --------------------------------------------------------------------------- #
# /new — divider behaviour (CCR-047: /clear collapsed into /new)
# --------------------------------------------------------------------------- #


async def test_cmd_new_running_stops_then_starts_fresh_with_divider(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CCR-047: ``/new`` over a running session stops it, posts the divider,
    then starts fresh."""
    manager = FakeManager(status=SessionStatus.RUNNING, pid=9999)
    await _seed_paired_user(
        session_factory,
        tg_user_id=1,
        last_chat_id=1001,
        is_owner=True,
    )
    await _seed_paired_user(session_factory, tg_user_id=2, last_chat_id=1002)
    bot_mock = AsyncMock()
    bot_mock.send_message = AsyncMock()
    msg = _make_message(text="/new", user_id=7, bot=bot_mock)

    await cmd_new(msg, session_manager=manager, db_factory=session_factory)

    manager.stop.assert_awaited_once_with()
    manager.new_session.assert_awaited_once_with(prompt=None, started_by_tg_user_id=7)
    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert reply == "New session started (pid 9999)."
    assert re.search(r"\(pid \d+\)", reply) is not None
    assert "11111111" not in reply

    assert bot_mock.send_message.await_count == 2
    sent_chat_ids = {call.args[0] for call in bot_mock.send_message.await_args_list}
    assert sent_chat_ids == {1001, 1002}
    for call in bot_mock.send_message.await_args_list:
        assert call.args[1] == _DIVIDER_MESSAGE


async def test_cmd_new_idle_skips_divider(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CCR-047: ``/new`` from a clean (IDLE) state posts no divider."""
    manager = FakeManager(status=SessionStatus.IDLE)
    await _seed_paired_user(
        session_factory,
        tg_user_id=1,
        last_chat_id=1001,
        is_owner=True,
    )
    await _seed_paired_user(session_factory, tg_user_id=2, last_chat_id=1002)

    async def _start(*, prompt: str | None, started_by_tg_user_id: int) -> uuid.UUID:
        del prompt, started_by_tg_user_id
        manager.set_status(SessionStatus.RUNNING)
        return _DEFAULT_SESSION_ID

    manager.new_session.side_effect = _start

    bot_mock = AsyncMock()
    bot_mock.send_message = AsyncMock()
    msg = _make_message(text="/new", user_id=7, bot=bot_mock)

    await cmd_new(msg, session_manager=manager, db_factory=session_factory)

    bot_mock.send_message.assert_not_awaited()
    manager.stop.assert_not_awaited()
    manager.new_session.assert_awaited_once_with(prompt=None, started_by_tg_user_id=7)
    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert "started" in reply


async def test_cmd_new_swallows_broadcast_failures(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CCR-047: a paired user with a broken chat does not break the divider broadcast."""
    manager = FakeManager(status=SessionStatus.RUNNING, pid=4321)
    await _seed_paired_user(
        session_factory,
        tg_user_id=1,
        last_chat_id=1001,
        is_owner=True,
    )
    await _seed_paired_user(session_factory, tg_user_id=2, last_chat_id=1002)

    async def _send(chat_id: int, text: str, **kwargs: object) -> None:
        del text, kwargs
        if chat_id == 1001:
            raise TelegramAPIError(method=None, message="blocked")  # type: ignore[arg-type]

    bot_mock = AsyncMock()
    bot_mock.send_message = AsyncMock(side_effect=_send)
    msg = _make_message(text="/new", user_id=7, bot=bot_mock)

    await cmd_new(msg, session_manager=manager, db_factory=session_factory)

    assert bot_mock.send_message.await_count == 2
    sent_chat_ids = [call.args[0] for call in bot_mock.send_message.await_args_list]
    assert 1002 in sent_chat_ids
    manager.new_session.assert_awaited_once_with(prompt=None, started_by_tg_user_id=7)
    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert "started" in reply


async def test_cmd_new_no_paired_users_with_chat_id_completes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CCR-047: paired user without ``last_chat_id`` → divider is suppressed for them, /new still completes."""
    manager = FakeManager(status=SessionStatus.RUNNING, pid=5555)
    await _seed_paired_user(
        session_factory,
        tg_user_id=1,
        last_chat_id=None,
        is_owner=True,
    )
    bot_mock = AsyncMock()
    bot_mock.send_message = AsyncMock()
    msg = _make_message(text="/new", user_id=7, bot=bot_mock)

    await cmd_new(msg, session_manager=manager, db_factory=session_factory)

    bot_mock.send_message.assert_not_awaited()
    manager.new_session.assert_awaited_once_with(prompt=None, started_by_tg_user_id=7)
    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert "started" in reply


# --------------------------------------------------------------------------- #
# /pid
# --------------------------------------------------------------------------- #


async def test_cmd_pid_idle_returns_no_active_session() -> None:
    manager = FakeManager(status=SessionStatus.IDLE)
    msg = _make_message(text="/pid")

    await cmd_pid(msg, session_manager=manager)

    msg.answer.assert_awaited_once_with("No active session.")


async def test_cmd_pid_active_returns_pid_and_uptime() -> None:
    """CCR-045: ``/pid`` reply exposes pid + uptime; the local-UUID prefix is dropped."""
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
    assert re.match(r"^Session running · pid \d+ · uptime \d", reply) is not None
    # Local-UUID 8-hex prefix MUST NOT appear in the reply.
    assert "abcdef01" not in reply
    assert "12345" in reply


async def test_cmd_pid_active_uptime_minutes_format() -> None:
    started_at = datetime.now(UTC) - timedelta(minutes=2, seconds=10)
    manager = FakeManager(status=SessionStatus.RUNNING, pid=777, started_at=started_at)
    msg = _make_message(text="/pid")

    await cmd_pid(msg, session_manager=manager)

    reply = msg.answer.await_args.args[0]
    assert re.search(r"uptime \d+m \d+s$", reply) is not None


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
    name: str | None = None,
    claude_session_id: str | None = None,
) -> None:
    async with session_factory() as db:
        db.add(
            Session(
                id=session_id,
                started_at=started_at,
                status=status,
                started_by_tg_user_id=started_by_tg_user_id,
                first_prompt=first_prompt,
                name=name,
                claude_session_id=claude_session_id,
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
    # CCR-042: ``/sessions`` renders ``claude_session_id[:8]`` (not the
    # local UUID), so seed each row with a distinguishing Claude id whose
    # prefix matches the local UUID's prefix for assertion convenience.
    cid_oldest = "aaaaaaaa-1111-2222-3333-444444440001"
    cid_middle = "bbbbbbbb-1111-2222-3333-444444440002"
    cid_newest = "cccccccc-1111-2222-3333-444444440003"
    await _seed_session(
        session_factory,
        session_id=sid_oldest,
        started_at=base,
        status="completed",
        first_prompt="oldest",
        claude_session_id=cid_oldest,
    )
    await _seed_session(
        session_factory,
        session_id=sid_middle,
        started_at=base + timedelta(seconds=10),
        status="stopped",
        first_prompt="middle",
        claude_session_id=cid_middle,
    )
    await _seed_session(
        session_factory,
        session_id=sid_newest,
        started_at=base + timedelta(seconds=20),
        status="crashed",
        first_prompt="newest",
        claude_session_id=cid_newest,
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


async def test_cmd_sessions_renders_unnamed_placeholder_when_name_is_null(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Rows with NULL ``Session.name`` render the stable ``(unnamed)`` placeholder."""
    sid = uuid.UUID("dddddddd-0000-0000-0000-000000000004")
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=datetime(2026, 4, 30, 13, 0, 0, tzinfo=UTC),
        status="completed",
        first_prompt="x" * 80,
        name=None,
    )

    msg = _make_message(text="/sessions")
    await cmd_sessions(msg, db_factory=session_factory)

    reply = msg.answer.await_args.args[0]
    assert "(unnamed)" in reply


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


async def test_cmd_sessions_renders_started_at_via_helper_short_format_utc(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Each session row's started_at renders via format_user_datetime("short"), UTC fallback."""
    sid = uuid.UUID("12345678-0000-0000-0000-000000000099")
    started_at = datetime(2026, 5, 5, 14, 3, 17, tzinfo=UTC)
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=started_at,
        status="completed",
        first_prompt="hi",
    )

    msg = _make_message(text="/sessions", user_id=42)
    await cmd_sessions(msg, db_factory=session_factory)

    reply = msg.answer.await_args.args[0]
    # New helper output ("short" mode, UTC since the caller is unpaired).
    assert "14:03 - 5 May" in reply
    # Old isoformat shape must NOT appear (regression vs. CCR-035 sweep).
    assert "2026-05-05T14:03:17" not in reply


async def test_cmd_sessions_applies_caller_timezone_to_started_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A paired caller with a non-UTC timezone shifts the rendered started_at."""
    # Seed a paired caller with Asia/Tokyo (UTC+9 year-round).
    await _seed_paired_user(
        session_factory,
        tg_user_id=42,
        last_chat_id=1000,
        is_owner=False,
    )
    async with session_factory() as db:
        u = (await db.scalars(select(PairedUser).where(PairedUser.tg_user_id == 42))).one()
        u.timezone = "Asia/Tokyo"
        await db.commit()

    sid = uuid.UUID("99999999-0000-0000-0000-000000000123")
    started_at = datetime(2026, 5, 5, 23, 30, 0, tzinfo=UTC)
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=started_at,
        status="completed",
        first_prompt="hi",
    )

    msg = _make_message(text="/sessions", user_id=42)
    await cmd_sessions(msg, db_factory=session_factory)

    reply = msg.answer.await_args.args[0]
    # Tokyo render of 2026-05-05 23:30 UTC → 08:30 next day (6 May).
    assert "08:30 - 6 May" in reply
    # The UTC-rendered string must NOT appear.
    assert "23:30 - 5 May" not in reply


# --------------------------------------------------------------------------- #
# CCR-037: /sessions five-field listing format.
# --------------------------------------------------------------------------- #


async def test_cmd_sessions_renders_five_field_format_with_claude_session_id_and_username(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fully-populated row renders as
    ``<claude_id_8> · <status> · <name> · HH:MM - D Mon · by @<username>``.

    CCR-042: the session-id slot is the first 8 chars of
    ``claude_session_id`` (NOT the full UUID, NOT the local row UUID).
    """
    await _seed_paired_user(
        session_factory,
        tg_user_id=77,
        last_chat_id=1077,
        is_owner=False,
    )
    sid = uuid.UUID("12345678-aaaa-bbbb-cccc-000000000001")
    claude_id = "deadbeef-1111-2222-3333-444455556666"
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=datetime(2026, 5, 6, 10, 15, 0, tzinfo=UTC),
        status="completed",
        started_by_tg_user_id=77,
        first_prompt="kicked off",
        name="Refactor pairing flow",
        claude_session_id=claude_id,
    )

    msg = _make_message(text="/sessions", user_id=77, username="u77")
    await cmd_sessions(msg, db_factory=session_factory)

    reply = msg.answer.await_args.args[0]
    # CCR-035 short format is "HH:MM - D Mon"; the ticket-spec shorthand
    # "HH:MM DD-MM" maps to that helper output via mode="short".
    expected = "<code>deadbeef</code> · completed · Refactor pairing flow · 10:15 - 6 May · by @u77"
    assert expected in reply
    # The full UUID must NOT appear — CCR-042 renders only the 8-char prefix.
    assert claude_id not in reply


async def test_cmd_sessions_renders_marker_for_null_claude_session_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CCR-042 fix-loop: rows with ``claude_session_id IS NULL`` render the
    literal marker ``--------`` (8 dashes) — explicitly NOT the local-UUID
    prefix.

    The previous CCR-042 pass used the local UUID's first 8 hex chars as a
    fallback, which the user-gate caught: those prefixes were unmatchable
    by ``/continue`` (the manager's prefix-match arm skips NULL rows), so
    the listing surfaced a token that ``/continue`` then rejected. The
    marker disambiguates the "this row isn't /continue-able" case from a
    real prefix.
    """
    sid = uuid.UUID("abcdef01-1111-2222-3333-444444444444")
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=datetime(2026, 5, 6, 11, 0, 0, tzinfo=UTC),
        status="completed",
        started_by_tg_user_id=42,
        first_prompt="legacy",
        name="legacy",
        claude_session_id=None,
    )

    msg = _make_message(text="/sessions", user_id=42)
    await cmd_sessions(msg, db_factory=session_factory)

    reply = msg.answer.await_args.args[0]
    assert "<code>--------</code>" in reply
    # The local-UUID prefix MUST NOT be rendered: that was the previous
    # fallback that misled users into typing it as a /continue argument.
    assert "<code>abcdef01</code>" not in reply
    assert "abcdef01" not in reply


async def test_cmd_sessions_alignment_invariant_marker_only_for_null_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CCR-042 fix-loop alignment invariant: every 8-char ``<code>...</code>``
    slot in ``/sessions`` is either the documented marker (``--------``) or
    the first 8 chars of a row's ``claude_session_id``.

    The prefix surface in ``/sessions`` and the resolution path in
    ``/continue`` (via :meth:`SessionManager._db_lookup_resumable_claude_session_id`)
    are aligned by construction: NULL rows render the marker (not a local
    UUID prefix), so any 8-hex token in the listing matches a non-NULL
    ``claude_session_id`` and is therefore resolvable.
    """
    null_local_id = uuid.UUID("aabbccdd-1111-2222-3333-444444444444")
    set_local_id = uuid.UUID("ffffeeee-1111-2222-3333-555555555555")
    set_claude_id = "12345678-9999-aaaa-bbbb-cccccccccccc"

    await _seed_session(
        session_factory,
        session_id=null_local_id,
        started_at=datetime(2026, 5, 6, 9, 0, 0, tzinfo=UTC),
        status="completed",
        started_by_tg_user_id=42,
        first_prompt="legacy",
        name="legacy",
        claude_session_id=None,
    )
    await _seed_session(
        session_factory,
        session_id=set_local_id,
        started_at=datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC),
        status="completed",
        started_by_tg_user_id=42,
        first_prompt="modern",
        name="modern",
        claude_session_id=set_claude_id,
    )

    msg = _make_message(text="/sessions", user_id=42)
    await cmd_sessions(msg, db_factory=session_factory)

    reply = msg.answer.await_args.args[0]
    # NULL row line surfaces the marker, NOT the local UUID prefix.
    assert "<code>--------</code>" in reply
    assert str(null_local_id)[:8] not in reply
    # Non-NULL row line surfaces the claude_session_id 8-hex prefix.
    assert f"<code>{set_claude_id[:8]}</code>" in reply
    # The local UUID of the non-NULL row also must not leak (the listing
    # never renders the local UUID, set or NULL).
    assert str(set_local_id)[:8] not in reply


async def test_cmd_sessions_resume_chain_rows_render_same_claude_id_prefix(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CCR-042: rows in a resume chain (sharing one ``claude_session_id``) all
    render the same 8-hex prefix in ``/sessions``.

    Mirrors the post-CCR-041 invariant that a ``--resume`` chain shares one
    Claude id across multiple ``Session`` rows; the listing surfaces that
    sharing visually so users can re-use the prefix with ``/continue``.
    """
    shared_claude_id = "fedcba98-7777-8888-9999-aaaabbbbcccc"
    older_id = uuid.UUID("11110000-0000-0000-0000-000000000001")
    newer_id = uuid.UUID("22220000-0000-0000-0000-000000000002")
    await _seed_session(
        session_factory,
        session_id=older_id,
        started_at=datetime(2026, 5, 6, 9, 0, 0, tzinfo=UTC),
        status="completed",
        started_by_tg_user_id=42,
        first_prompt="first",
        name="first",
        claude_session_id=shared_claude_id,
    )
    await _seed_session(
        session_factory,
        session_id=newer_id,
        started_at=datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC),
        status="completed",
        started_by_tg_user_id=42,
        first_prompt="resumed",
        name="resumed",
        claude_session_id=shared_claude_id,
    )

    msg = _make_message(text="/sessions", user_id=42)
    await cmd_sessions(msg, db_factory=session_factory)

    reply = msg.answer.await_args.args[0]
    # Both rows show the same 8-char prefix — count occurrences of the
    # specific ``<code>fedcba98</code>`` rendering.
    assert reply.count("<code>fedcba98</code>") == 2
    # The full id MUST NOT leak.
    assert shared_claude_id not in reply


async def test_cmd_sessions_falls_back_to_tg_user_id_when_username_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Rows started by a paired user with no ``tg_username`` render ``by <tg_user_id>``."""
    # Seed a paired user explicitly with NULL username.
    async with session_factory() as db:
        db.add(
            PairedUser(
                tg_user_id=555,
                tg_username=None,
                is_owner=False,
                approved_at=datetime.now(UTC),
                last_chat_id=2000,
            ),
        )
        await db.commit()

    sid = uuid.UUID("99999999-aaaa-bbbb-cccc-000000000099")
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC),
        status="completed",
        started_by_tg_user_id=555,
        first_prompt="anon",
        name="anon",
    )

    msg = _make_message(text="/sessions", user_id=555, username=None)
    await cmd_sessions(msg, db_factory=session_factory)

    reply = msg.answer.await_args.args[0]
    assert "by 555" in reply
    # No leading "@" should appear before the user id.
    assert "by @555" not in reply


async def test_cmd_sessions_falls_back_to_tg_user_id_when_no_paired_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Rows started by a tg_user_id with no matching ``paired_users`` row also fall back."""
    sid = uuid.UUID("88888888-aaaa-bbbb-cccc-000000000088")
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=datetime(2026, 5, 6, 12, 30, 0, tzinfo=UTC),
        status="completed",
        started_by_tg_user_id=999,
        first_prompt="lone",
        name="lone",
    )

    msg = _make_message(text="/sessions", user_id=42)
    await cmd_sessions(msg, db_factory=session_factory)

    reply = msg.answer.await_args.args[0]
    assert "by 999" in reply


async def test_cmd_sessions_html_escapes_name_with_special_chars(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A session name with ``<``, ``>``, ``&`` is HTML-escaped on render."""
    sid = uuid.UUID("77777777-aaaa-bbbb-cccc-000000000077")
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=datetime(2026, 5, 6, 13, 0, 0, tzinfo=UTC),
        status="completed",
        started_by_tg_user_id=42,
        first_prompt="x",
        name="<script> & </script>",
    )

    msg = _make_message(text="/sessions", user_id=42)
    await cmd_sessions(msg, db_factory=session_factory)

    reply = msg.answer.await_args.args[0]
    # Raw angle brackets MUST NOT appear inside the name slot — they
    # would be parsed as HTML tags by Telegram and break the message.
    assert "&lt;script&gt;" in reply
    assert "&amp;" in reply
    assert "<script>" not in reply


# --------------------------------------------------------------------------- #
# CCR-037: /rename
# --------------------------------------------------------------------------- #


async def test_cmd_rename_overwrites_null_name(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # CCR-043: prefix is matched against ``claude_session_id[:8]``, not the
    # local-row UUID prefix. Seed both so the prefix in the message
    # corresponds to the Claude id.
    sid = uuid.UUID("11112222-3333-4444-5555-666677778888")
    cid = "deadbeef-1111-2222-3333-444455556666"
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=datetime(2026, 5, 6, 14, 0, 0, tzinfo=UTC),
        status="completed",
        first_prompt="hello",
        name=None,
        claude_session_id=cid,
    )
    manager = FakeManager(status=SessionStatus.IDLE)

    msg = _make_message(text="/rename deadbeef New label", user_id=42)
    await cmd_rename(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert "deadbeef" in reply
    assert "New label" in reply

    async with session_factory() as db:
        row = await db.scalar(select(Session).where(Session.id == sid))
    assert row is not None
    assert row.name == "New label"


async def test_cmd_rename_overwrites_existing_name(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``/rename`` overwrites a non-NULL value (manual rename always wins)."""
    sid = uuid.UUID("aaaa1111-2222-3333-4444-555566667777")
    cid = "feedface-1111-2222-3333-444455556666"
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=datetime(2026, 5, 6, 14, 30, 0, tzinfo=UTC),
        status="completed",
        first_prompt="hello",
        name="old name",
        claude_session_id=cid,
    )
    manager = FakeManager(status=SessionStatus.IDLE)

    msg = _make_message(text="/rename feedface brand new", user_id=42)
    await cmd_rename(msg, session_manager=manager, db_factory=session_factory)

    async with session_factory() as db:
        row = await db.scalar(select(Session).where(Session.id == sid))
    assert row is not None
    assert row.name == "brand new"


def test_cmd_rename_usage_hint_is_html_safe() -> None:
    assert "<" not in _RENAME_USAGE_HINT
    assert ">" not in _RENAME_USAGE_HINT


def test_cmd_rename_usage_hint_documents_both_forms() -> None:
    """CCR-043: the usage hint must document both the prefix and ``current`` forms."""
    assert "&lt;8-hex-prefix&gt;" in _RENAME_USAGE_HINT
    assert "&lt;name&gt;" in _RENAME_USAGE_HINT
    assert "current" in _RENAME_USAGE_HINT


async def test_cmd_rename_no_args_returns_usage_hint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = FakeManager(status=SessionStatus.IDLE)
    msg = _make_message(text="/rename", user_id=42)
    await cmd_rename(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert reply.startswith("Usage: /rename")


async def test_cmd_rename_only_prefix_returns_usage_hint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = FakeManager(status=SessionStatus.IDLE)
    msg = _make_message(text="/rename 11112222", user_id=42)
    await cmd_rename(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert reply.startswith("Usage: /rename")


async def test_cmd_rename_blank_name_returns_usage_hint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = FakeManager(status=SessionStatus.IDLE)
    msg = _make_message(text="/rename 11112222    ", user_id=42)
    await cmd_rename(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert reply.startswith("Usage: /rename")


async def test_cmd_rename_unknown_prefix_returns_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sid = uuid.UUID("aabbccdd-1111-2222-3333-444455556666")
    cid = "abcdef00-1111-2222-3333-444455556666"
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=datetime(2026, 5, 6, 15, 0, 0, tzinfo=UTC),
        status="completed",
        first_prompt="x",
        claude_session_id=cid,
    )
    manager = FakeManager(status=SessionStatus.IDLE)

    msg = _make_message(text="/rename deadbeef whatever", user_id=42)
    await cmd_rename(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert "No session found" in reply
    assert "deadbeef" in reply


async def test_cmd_rename_invalid_prefix_returns_usage_hint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A prefix that is not 8 hex chars returns the usage hint, no DB hit."""
    manager = FakeManager(status=SessionStatus.IDLE)
    msg = _make_message(text="/rename ZZZZZZZZ name", user_id=42)
    await cmd_rename(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert reply.startswith("Usage: /rename")


async def test_cmd_rename_truncates_long_name_to_40_chars(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sid = uuid.UUID("ccccdddd-1111-2222-3333-444455556666")
    cid = "cafef00d-1111-2222-3333-444455556666"
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=datetime(2026, 5, 6, 15, 30, 0, tzinfo=UTC),
        status="completed",
        first_prompt="x",
        claude_session_id=cid,
    )
    manager = FakeManager(status=SessionStatus.IDLE)
    long_name = "x" * 80

    msg = _make_message(text=f"/rename cafef00d {long_name}", user_id=42)
    await cmd_rename(msg, session_manager=manager, db_factory=session_factory)

    async with session_factory() as db:
        row = await db.scalar(select(Session).where(Session.id == sid))
    assert row is not None
    assert row.name is not None
    assert len(row.name) <= 40


async def test_cmd_rename_resume_chain_renames_all_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CCR-043: a prefix that matches multiple rows in a resume chain
    (rows sharing one ``claude_session_id``) renames every row in the
    chain in one commit so the ``/sessions`` listing stays consistent.
    """
    shared_cid = "fedcba98-7777-8888-9999-aaaabbbbcccc"
    older_id = uuid.UUID("11110000-0000-0000-0000-000000000001")
    newer_id = uuid.UUID("22220000-0000-0000-0000-000000000002")
    await _seed_session(
        session_factory,
        session_id=older_id,
        started_at=datetime(2026, 5, 6, 9, 0, 0, tzinfo=UTC),
        status="completed",
        first_prompt="first",
        name="first-name",
        claude_session_id=shared_cid,
    )
    await _seed_session(
        session_factory,
        session_id=newer_id,
        started_at=datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC),
        status="completed",
        first_prompt="resumed",
        name="second-name",
        claude_session_id=shared_cid,
    )
    manager = FakeManager(status=SessionStatus.IDLE)

    msg = _make_message(text="/rename fedcba98 newname", user_id=42)
    await cmd_rename(msg, session_manager=manager, db_factory=session_factory)

    async with session_factory() as db:
        older = await db.scalar(select(Session).where(Session.id == older_id))
        newer = await db.scalar(select(Session).where(Session.id == newer_id))
    assert older is not None
    assert newer is not None
    assert older.name == "newname"
    assert newer.name == "newname"


async def test_cmd_rename_skips_null_claude_session_id_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CCR-043: rows with NULL ``claude_session_id`` are excluded from
    prefix matching. A request whose prefix happens to equal the local
    UUID prefix of a NULL row must NOT mutate the row — it returns the
    unknown-prefix error instead.
    """
    sid = uuid.UUID("abcdef01-1111-2222-3333-444444444444")
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=datetime(2026, 5, 6, 11, 0, 0, tzinfo=UTC),
        status="completed",
        first_prompt="legacy",
        name="legacy-name",
        claude_session_id=None,
    )
    manager = FakeManager(status=SessionStatus.IDLE)

    # The prefix matches the local UUID's first 8 chars — under the old
    # CCR-037 behaviour that would have matched. CCR-043 excludes NULL
    # rows, so the call returns the unknown-prefix error.
    msg = _make_message(text="/rename abcdef01 newname", user_id=42)
    await cmd_rename(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert "No session found" in reply
    assert "abcdef01" in reply

    async with session_factory() as db:
        row = await db.scalar(select(Session).where(Session.id == sid))
    assert row is not None
    assert row.name == "legacy-name"


async def test_cmd_rename_current_no_active_session_replies_no_active(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CCR-043: ``/rename current`` with no subprocess held replies with
    the exact ``"No active session to rename."`` string and does not mutate.
    """
    sid = uuid.UUID("11112222-3333-4444-5555-666677778888")
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=datetime(2026, 5, 6, 14, 0, 0, tzinfo=UTC),
        status="completed",
        first_prompt="hello",
        name="old",
        claude_session_id="deadbeef-1111-2222-3333-444455556666",
    )
    # FakeManager defaults to IDLE with all-None info() snapshot.
    manager = FakeManager(status=SessionStatus.IDLE)
    msg = _make_message(text="/rename current newname", user_id=42)

    await cmd_rename(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once_with("No active session to rename.")

    async with session_factory() as db:
        row = await db.scalar(select(Session).where(Session.id == sid))
    assert row is not None
    assert row.name == "old"


async def test_cmd_rename_current_idle_active_session_replies_distinct_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CCR-043: ``/rename current`` during the CCR-044 ``[idle]`` window
    (subprocess held but ``SystemInit`` not yet fired) replies with a
    distinct error string — explicitly NOT ``"No active session to rename."``
    — and does not mutate.
    """
    sid = uuid.UUID("11112222-3333-4444-5555-666677778888")
    await _seed_session(
        session_factory,
        session_id=sid,
        started_at=datetime(2026, 5, 6, 14, 0, 0, tzinfo=UTC),
        status="idle",
        first_prompt=None,
        name="old",
        claude_session_id=None,
    )
    # IDLE + non-None session_id mimics the CCR-044 idle window.
    manager = FakeManager(
        status=SessionStatus.IDLE,
        pid=1234,
        session_id=sid,
    )
    # Override info() to return the idle-window snapshot regardless of the
    # default IDLE all-None branch.
    started_at = datetime.now(UTC)

    async def _idle_window_info() -> dict[str, object]:
        return {
            "session_id": sid,
            "pid": 1234,
            "started_at": started_at,
            "status": SessionStatus.IDLE,
        }

    manager.info = _idle_window_info  # type: ignore[method-assign]
    msg = _make_message(text="/rename current newname", user_id=42)

    await cmd_rename(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert reply != "No active session to rename."
    assert "claude_session_id" in reply or "yet" in reply

    async with session_factory() as db:
        row = await db.scalar(select(Session).where(Session.id == sid))
    assert row is not None
    assert row.name == "old"


async def test_cmd_rename_current_running_renames_chain(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CCR-043: ``/rename current`` with a running session and a populated
    ``claude_session_id`` renames every row sharing that id (resume chain).
    """
    shared_cid = "fedcba98-7777-8888-9999-aaaabbbbcccc"
    older_id = uuid.UUID("11110000-0000-0000-0000-000000000001")
    newer_id = uuid.UUID("22220000-0000-0000-0000-000000000002")
    await _seed_session(
        session_factory,
        session_id=older_id,
        started_at=datetime(2026, 5, 6, 9, 0, 0, tzinfo=UTC),
        status="completed",
        first_prompt="first",
        name="first-name",
        claude_session_id=shared_cid,
    )
    await _seed_session(
        session_factory,
        session_id=newer_id,
        started_at=datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC),
        status="running",
        first_prompt="resumed",
        name="second-name",
        claude_session_id=shared_cid,
    )
    manager = FakeManager(
        status=SessionStatus.RUNNING,
        pid=1234,
        session_id=newer_id,
    )

    msg = _make_message(text="/rename current the chain", user_id=42)
    await cmd_rename(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert "renamed to" in reply
    assert "the chain" in reply
    assert "fedcba98" in reply

    async with session_factory() as db:
        older = await db.scalar(select(Session).where(Session.id == older_id))
        newer = await db.scalar(select(Session).where(Session.id == newer_id))
    assert older is not None
    assert newer is not None
    assert older.name == "the chain"
    assert newer.name == "the chain"


async def test_cmd_rename_current_no_name_returns_usage_hint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CCR-043: ``/rename current`` without a name returns the usage hint."""
    manager = FakeManager(status=SessionStatus.IDLE)
    msg = _make_message(text="/rename current", user_id=42)
    await cmd_rename(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert reply.startswith("Usage: /rename")
