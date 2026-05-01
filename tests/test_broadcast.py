"""Tests for ``ccr.server._broadcast_loop``.

We drive the loop directly with an in-memory DB and an :class:`AsyncMock`
``Bot``: publish one ``AssistantTurn`` to the bus and assert that
``bot.send_message`` was called once per paired user with a known
``last_chat_id``. Users with ``last_chat_id IS NULL`` are filtered out.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest_asyncio
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ccr.claude.events import AssistantTurn, TextBlock
from ccr.db.engine import AsyncSessionMaker
from ccr.db.models import Base, PairedUser
from ccr.events import EventBus
from ccr.server import _broadcast_loop

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine


_SESSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


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


async def _seed_user(
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


def _make_text_event(text: str = "hello") -> AssistantTurn:
    return AssistantTurn.model_validate(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [TextBlock(type="text", text=text).model_dump()],
            },
        },
    )


async def _wait_for_call_count(mock: AsyncMock, count: int, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while mock.await_count < count:
        if asyncio.get_running_loop().time() > deadline:
            msg = f"only saw {mock.await_count} calls, expected {count}"
            raise AssertionError(msg)
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------- #
# Tests.
# --------------------------------------------------------------------------- #


async def test_broadcast_fans_out_to_three_paired_users(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_user(session_factory, tg_user_id=1, last_chat_id=1001, is_owner=True)
    await _seed_user(session_factory, tg_user_id=2, last_chat_id=1002)
    await _seed_user(session_factory, tg_user_id=3, last_chat_id=1003)

    bus = EventBus()
    bot = AsyncMock()
    bot.send_message = AsyncMock()

    loop_task = asyncio.create_task(_broadcast_loop(bus, bot, session_factory))
    # Yield so the subscriber registers before we publish.
    await asyncio.sleep(0)

    event = _make_text_event("ping")
    await bus.publish(
        "session.event",
        {"session_id": _SESSION_ID, "seq": 0, "event": event},
    )

    await _wait_for_call_count(bot.send_message, 3)

    chat_ids = {call.args[0] for call in bot.send_message.await_args_list}
    assert chat_ids == {1001, 1002, 1003}
    for call in bot.send_message.await_args_list:
        assert call.args[1] == "ping"
        assert call.kwargs.get("parse_mode") == "HTML"
        assert call.kwargs.get("reply_markup") is None

    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task


async def test_broadcast_skips_users_without_chat_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_user(session_factory, tg_user_id=1, last_chat_id=1001, is_owner=True)
    await _seed_user(session_factory, tg_user_id=2, last_chat_id=None)

    bus = EventBus()
    bot = AsyncMock()
    bot.send_message = AsyncMock()

    loop_task = asyncio.create_task(_broadcast_loop(bus, bot, session_factory))
    await asyncio.sleep(0)

    await bus.publish(
        "session.event",
        {"session_id": _SESSION_ID, "seq": 0, "event": _make_text_event("ping")},
    )

    await _wait_for_call_count(bot.send_message, 1)
    chat_ids = {call.args[0] for call in bot.send_message.await_args_list}
    assert chat_ids == {1001}

    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task


async def test_broadcast_skips_revoked_users(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_user(session_factory, tg_user_id=1, last_chat_id=1001, is_owner=True)

    # Revoked friend with a known chat id — must be skipped.
    async with session_factory() as db:
        db.add(
            PairedUser(
                tg_user_id=2,
                tg_username="revoked",
                is_owner=False,
                approved_at=datetime.now(UTC),
                last_chat_id=2002,
                revoked_at=datetime.now(UTC),
            ),
        )
        await db.commit()

    bus = EventBus()
    bot = AsyncMock()
    bot.send_message = AsyncMock()

    loop_task = asyncio.create_task(_broadcast_loop(bus, bot, session_factory))
    await asyncio.sleep(0)

    await bus.publish(
        "session.event",
        {"session_id": _SESSION_ID, "seq": 0, "event": _make_text_event("ping")},
    )
    await _wait_for_call_count(bot.send_message, 1)
    chat_ids = {call.args[0] for call in bot.send_message.await_args_list}
    assert chat_ids == {1001}

    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task


async def test_broadcast_swallows_telegram_errors_per_chat(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One failing chat must not stop delivery to others."""
    await _seed_user(session_factory, tg_user_id=1, last_chat_id=1001, is_owner=True)
    await _seed_user(session_factory, tg_user_id=2, last_chat_id=1002)

    bus = EventBus()
    bot = AsyncMock()

    async def _send(chat_id: int, _text: str, **_kwargs: object) -> None:
        if chat_id == 1001:
            raise TelegramAPIError(method=None, message="blocked")  # type: ignore[arg-type]

    bot.send_message = AsyncMock(side_effect=_send)

    loop_task = asyncio.create_task(_broadcast_loop(bus, bot, session_factory))
    await asyncio.sleep(0)

    await bus.publish(
        "session.event",
        {"session_id": _SESSION_ID, "seq": 0, "event": _make_text_event("ping")},
    )

    # Both senders run; the failing one raises but that should not stop the other.
    await _wait_for_call_count(bot.send_message, 2)

    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task


async def test_broadcast_skips_events_with_no_messages(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SystemInit events render to no Telegram message and must not trigger sends."""
    from ccr.claude.events import SystemInit

    await _seed_user(session_factory, tg_user_id=1, last_chat_id=1001, is_owner=True)

    bus = EventBus()
    bot = AsyncMock()
    bot.send_message = AsyncMock()

    loop_task = asyncio.create_task(_broadcast_loop(bus, bot, session_factory))
    await asyncio.sleep(0)

    await bus.publish(
        "session.event",
        {"session_id": _SESSION_ID, "seq": 0, "event": SystemInit(type="system", subtype="init")},
    )
    # Give the loop a moment to (not) act.
    await asyncio.sleep(0.05)
    bot.send_message.assert_not_called()

    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task
