"""Tests for the ``/continue`` handler.

Mirrors :mod:`tests.test_bot_session`'s ``FakeManager`` pattern: we call
the handler directly with a stubbed :class:`SessionManager` and an
in-memory DB factory so the dispatch logic is exercised without needing
a real Telegram bot.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest_asyncio
from aiogram.types import Chat, Message, User
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ccr.bot.handlers.session import cmd_continue
from ccr.claude.manager import (
    NoPriorSessionError,
    SessionAlreadyRunningError,
    SessionError,
    SessionNotFoundError,
)
from ccr.claude.state import SessionStatus
from ccr.db.engine import AsyncSessionMaker
from ccr.db.models import Base

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
    text: str = "/continue",
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
    """Stub :class:`SessionManager` exposing only what ``cmd_continue`` uses."""

    def __init__(
        self,
        *,
        status: SessionStatus = SessionStatus.RUNNING,
        pid: int | None = 4242,
        session_id: uuid.UUID = _DEFAULT_SESSION_ID,
    ) -> None:
        self._status = status
        self._pid = pid
        self._session_id = session_id
        self.continue_session = AsyncMock(return_value=self._session_id)

    async def status(self) -> SessionStatus:
        return self._status

    async def info(self) -> dict[str, object]:
        return {
            "session_id": self._session_id,
            "pid": self._pid,
            "started_at": datetime.now(UTC),
            "status": self._status,
        }


# --------------------------------------------------------------------------- #
# Tests.
# --------------------------------------------------------------------------- #


async def test_cmd_continue_happy_path_replies_resumed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The happy path replies ``"Session <id8> resumed (pid <N>)."``."""
    manager = FakeManager(pid=4242)
    msg = _make_message(user_id=42)

    await cmd_continue(msg, session_manager=manager, db_factory=session_factory)

    manager.continue_session.assert_awaited_once_with(
        started_by_tg_user_id=42,
        session_id_prefix=None,
    )
    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert re.match(r"^Session [0-9a-f]{8} resumed \(pid \d+\)\.$", reply) is not None
    assert reply == "Session 11111111 resumed (pid 4242)."


async def test_cmd_continue_already_running_replies_canned_string(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``SessionAlreadyRunningError`` produces the documented canned string verbatim."""
    manager = FakeManager()
    manager.continue_session.side_effect = SessionAlreadyRunningError(
        "Session already running. /stop first or /clear to start fresh.",
    )
    msg = _make_message()

    await cmd_continue(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once_with(
        "Session already running. /stop first or /clear to start fresh.",
    )


async def test_cmd_continue_no_prior_session_replies_canned_string(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``NoPriorSessionError`` produces the documented canned string verbatim."""
    manager = FakeManager()
    manager.continue_session.side_effect = NoPriorSessionError(
        "No prior session to continue.",
    )
    msg = _make_message()

    await cmd_continue(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once_with("No prior session to continue.")


async def test_cmd_continue_other_session_error_html_escapes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unexpected ``SessionError`` messages are HTML-escaped before reply."""
    manager = FakeManager()
    manager.continue_session.side_effect = SessionError(
        "claude binary not found: </path>",
    )
    msg = _make_message()

    await cmd_continue(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert "&lt;/path&gt;" in reply
    assert "</path>" not in reply


# --------------------------------------------------------------------------- #
# CCR-020 extension: optional 8-hex prefix argument.
# --------------------------------------------------------------------------- #


async def test_cmd_continue_with_valid_prefix_calls_manager_with_prefix(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A valid 8-hex argument is forwarded to the manager via ``session_id_prefix``."""
    manager = FakeManager(pid=4242)
    msg = _make_message(text="/continue 76581b99", user_id=42)

    await cmd_continue(msg, session_manager=manager, db_factory=session_factory)

    manager.continue_session.assert_awaited_once_with(
        started_by_tg_user_id=42,
        session_id_prefix="76581b99",
    )
    msg.answer.assert_awaited_once()
    reply = msg.answer.await_args.args[0]
    assert reply == "Session 11111111 resumed (pid 4242)."


async def test_cmd_continue_with_invalid_format_replies_canned_string_no_manager_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Non-empty arguments that do not match ``^[0-9a-f]{8}$`` get a canned reply and no manager call."""
    bad_args = ["xyz", "12345", "123456789", "ABCDEFGH", "7g581b99"]
    for bad in bad_args:
        manager = FakeManager()
        msg = _make_message(text=f"/continue {bad}")

        await cmd_continue(msg, session_manager=manager, db_factory=session_factory)

        manager.continue_session.assert_not_awaited()
        msg.answer.assert_awaited_once_with(
            "Invalid session id. Expected 8 hex characters (e.g. /continue 76581b99).",
        )


async def test_cmd_continue_with_unknown_prefix_replies_not_found(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``SessionNotFoundError`` produces the documented canned string verbatim."""
    manager = FakeManager()
    manager.continue_session.side_effect = SessionNotFoundError(
        "No session found with id 00000000.",
    )
    msg = _make_message(text="/continue 00000000")

    await cmd_continue(msg, session_manager=manager, db_factory=session_factory)

    msg.answer.assert_awaited_once_with("No session found with id 00000000.")
