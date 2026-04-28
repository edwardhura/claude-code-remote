"""Tests for the `/start` handler and the allowlist middleware.

Strategy: use real in-memory SQLite (matches `tests/test_pairing.py`),
fabricate aiogram :class:`Message` objects, and patch the ``answer`` method
with :class:`AsyncMock` so we never touch the network. Where the handler
itself imports :func:`notify_owner`, we patch the imported name in
``ccr.bot.handlers.pairing`` so the production helper isn't exercised in
the bootstrap path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest_asyncio
from aiogram.types import Chat, Message, TelegramObject, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ccr.auth.pairing import approve, create_code
from ccr.bot.handlers.pairing import cmd_start
from ccr.bot.middlewares import AllowlistMiddleware
from ccr.db.engine import AsyncSessionMaker
from ccr.db.models import Base, PairedUser

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest
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


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


def _make_message(
    *,
    text: str,
    user_id: int = 42,
    username: str | None = "alice",
    chat_id: int = 1000,
) -> Message:
    """Build a Message with `answer` replaced by an AsyncMock."""
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="A", username=username),
        text=text,
    )
    # Replace the network-touching method with a mock for assertions.
    object.__setattr__(msg, "answer", AsyncMock())
    return msg


async def _seed_owner(
    factory: async_sessionmaker[AsyncSession],
    *,
    tg_user_id: int,
    last_chat_id: int | None,
) -> None:
    """Approve a fresh code for `tg_user_id` (auto-promotes to owner) and stamp last_chat_id."""
    async with factory() as session:
        pc = await create_code(session, tg_user_id=tg_user_id, tg_username="owner")
        await session.commit()
    async with factory() as session:
        await approve(session, pc.code)
    if last_chat_id is not None:
        async with factory() as session:
            row = await session.scalar(
                select(PairedUser).where(PairedUser.tg_user_id == tg_user_id),
            )
            assert row is not None
            row.last_chat_id = last_chat_id
            await session.commit()


# --------------------------------------------------------------------------- #
# /start handler tests (call the handler directly).
# --------------------------------------------------------------------------- #


async def test_start_bootstrap(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No owner in DB → handler replies with the code + approve command, no notify_owner."""
    bot_mock = AsyncMock()
    notify_mock = AsyncMock()
    monkeypatch.setattr("ccr.bot.handlers.pairing.notify_owner", notify_mock)

    msg = _make_message(text="/start", user_id=4242, username="alice")

    await cmd_start(
        msg,
        db_factory=session_factory,
        bot=bot_mock,
        is_paired_user=False,
    )

    msg.answer.assert_awaited_once()
    call = msg.answer.await_args
    assert call is not None
    reply_text = call.args[0] if call.args else call.kwargs["text"]
    assert "Bootstrap pairing" in reply_text
    assert "4242" in reply_text  # tg_user_id visible
    assert "python -m ccr pair approve" in reply_text
    notify_mock.assert_not_called()


async def test_start_normal_path(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner already exists → handler replies access-requested AND calls notify_owner with the code."""
    await _seed_owner(session_factory, tg_user_id=111, last_chat_id=999)

    bot_mock = AsyncMock()
    notify_mock = AsyncMock()
    monkeypatch.setattr("ccr.bot.handlers.pairing.notify_owner", notify_mock)

    msg = _make_message(text="/start", user_id=222, username="bob")

    await cmd_start(
        msg,
        db_factory=session_factory,
        bot=bot_mock,
        is_paired_user=False,
    )

    msg.answer.assert_awaited_once_with("Access requested. The owner has been notified.")
    notify_mock.assert_awaited_once()
    notify_call = notify_mock.await_args
    assert notify_call is not None
    notify_message: str = notify_call.args[2]
    assert "python -m ccr pair approve" in notify_message
    # Pairing code is 8 hex chars; rsplit once on "approve " then split once on "</" to isolate it.
    code_token = (
        notify_message.rsplit("approve ", maxsplit=1)[-1].split("</", maxsplit=1)[0].strip()
    )
    assert len(code_token) == 8


async def test_start_already_paired(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paired user → handler replies with the paired status string."""
    bot_mock = AsyncMock()
    notify_mock = AsyncMock()
    monkeypatch.setattr("ccr.bot.handlers.pairing.notify_owner", notify_mock)

    msg = _make_message(text="/start", user_id=111, username="alice")

    await cmd_start(
        msg,
        db_factory=session_factory,
        bot=bot_mock,
        is_paired_user=True,
    )

    msg.answer.assert_awaited_once_with(
        "Paired. Send a prompt to start, or /new for a fresh session."
    )
    notify_mock.assert_not_called()


# --------------------------------------------------------------------------- #
# Middleware tests.
# --------------------------------------------------------------------------- #


async def test_unpaired_plain_text_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unpaired plain text → middleware short-circuits with the rejection string; handler never runs."""
    middleware = AllowlistMiddleware()
    handler_mock = AsyncMock()

    msg = _make_message(text="hello world", user_id=4242, username="stranger")

    data: dict[str, Any] = {"db_factory": session_factory}
    result = await middleware(handler_mock, msg, data)

    assert result is None
    handler_mock.assert_not_called()
    msg.answer.assert_awaited_once_with("Not paired. Send /start to request access.")
    assert data["is_paired_user"] is False


async def test_middleware_paired_user_is_passed_through_and_chat_id_persisted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Paired user → middleware injects flag, persists last_chat_id, calls handler."""
    await _seed_owner(session_factory, tg_user_id=111, last_chat_id=None)

    middleware = AllowlistMiddleware()
    handler_mock = AsyncMock(return_value="ok")

    msg = _make_message(text="hello", user_id=111, username="owner", chat_id=5050)
    data: dict[str, Any] = {"db_factory": session_factory}

    result = await middleware(handler_mock, msg, data)

    assert result == "ok"
    handler_mock.assert_awaited_once()
    assert data["is_paired_user"] is True

    async with session_factory() as session:
        row = await session.scalar(
            select(PairedUser).where(PairedUser.tg_user_id == 111),
        )
        assert row is not None
        assert row.last_chat_id == 5050


async def test_middleware_unpaired_start_passes_through(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unpaired sender on `/start` is allowed past so the bootstrap branch can run."""
    middleware = AllowlistMiddleware()
    handler_mock = AsyncMock(return_value="reached")

    msg = _make_message(text="/start", user_id=4242, username="stranger")
    data: dict[str, Any] = {"db_factory": session_factory}

    result = await middleware(handler_mock, msg, data)

    assert result == "reached"
    handler_mock.assert_awaited_once()
    assert data["is_paired_user"] is False
    msg.answer.assert_not_awaited()


async def test_middleware_ignores_non_message_non_callback(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Updates with no extractable sender flow straight through the middleware."""
    middleware = AllowlistMiddleware()
    handler_mock = AsyncMock(return_value="ok")

    event = TelegramObject()  # no from_user / chat
    data: dict[str, Any] = {"db_factory": session_factory}

    result = await middleware(handler_mock, event, data)
    assert result == "ok"
    handler_mock.assert_awaited_once()
