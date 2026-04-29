"""Tests for ``ccr.bot.typing.TypingKeepalive`` and ``ccr.server._typing_loop``.

We monkeypatch :data:`ccr.bot.typing.TYPING_INTERVAL` to ``0`` so the 4 s
inter-iteration sleep doesn't stretch unit tests into multi-second runs.
The keepalive logic is otherwise exercised verbatim.
"""

from __future__ import annotations

import asyncio
import contextlib
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

from ccr.bot import typing as typing_module
from ccr.bot.typing import TypingKeepalive
from ccr.claude.events import AssistantTurn, ResultEvent, TextBlock
from ccr.db.engine import AsyncSessionMaker
from ccr.db.models import Base, PairedUser
from ccr.events import EventBus
from ccr.server import _typing_loop

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
            message = f"only saw {mock.await_count} calls, expected {count}"
            raise AssertionError(message)
        await asyncio.sleep(0.01)


@pytest_asyncio.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace :func:`asyncio.sleep` inside ``ccr.bot.typing`` with a near-zero one.

    The keepalive's normal 4 s interval would slow tests to a crawl; tests
    only need the loop to keep iterating. We monkey-patch only the
    ``TYPING_INTERVAL`` constant rather than ``asyncio.sleep`` itself so the
    rest of the module (and the test loop's own ``asyncio.sleep`` calls)
    behave normally.
    """
    monkeypatch.setattr(typing_module, "TYPING_INTERVAL", 0.0)


# --------------------------------------------------------------------------- #
# TypingKeepalive — unit tests.
# --------------------------------------------------------------------------- #


async def test_keepalive_fires(fast_sleep: None) -> None:
    del fast_sleep  # fixture has the side-effect we want
    bot = AsyncMock()
    bot.send_chat_action = AsyncMock()
    kv = TypingKeepalive(bot=bot, chat_id=42)

    kv.start()
    await _wait_for_call_count(bot.send_chat_action, 1)

    kv.cancel()
    await kv.wait_closed()

    # Each call must use the right chat id and "typing" action.
    assert bot.send_chat_action.await_args_list[0].args == (42, "typing")


async def test_keepalive_loops_multiple_times(fast_sleep: None) -> None:
    del fast_sleep
    bot = AsyncMock()
    bot.send_chat_action = AsyncMock()
    kv = TypingKeepalive(bot=bot, chat_id=7)

    kv.start()
    # The fast_sleep fixture rewrites the inter-iteration sleep to 0 seconds,
    # so the loop iterates as fast as the event loop runs cooperative tasks.
    await _wait_for_call_count(bot.send_chat_action, 5)

    kv.cancel()
    await kv.wait_closed()


async def test_keepalive_swallows_telegram_error(fast_sleep: None) -> None:
    """A ``TelegramAPIError`` from one iteration must not break the loop."""
    del fast_sleep
    bot = AsyncMock()
    calls = {"n": 0}

    async def _action(_chat_id: int, _action_name: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TelegramAPIError(method=None, message="rate limited")  # type: ignore[arg-type]

    bot.send_chat_action = AsyncMock(side_effect=_action)
    kv = TypingKeepalive(bot=bot, chat_id=99)

    kv.start()
    # The first iteration raises; subsequent iterations should still happen.
    await _wait_for_call_count(bot.send_chat_action, 3)

    kv.cancel()
    await kv.wait_closed()

    # The task must not have crashed mid-flight; at least one successful
    # subsequent call landed.
    assert bot.send_chat_action.await_count >= 3


async def test_keepalive_cancel_idempotent(fast_sleep: None) -> None:
    del fast_sleep
    bot = AsyncMock()
    bot.send_chat_action = AsyncMock()
    kv = TypingKeepalive(bot=bot, chat_id=5)

    kv.start()
    await asyncio.sleep(0)
    kv.cancel()
    kv.cancel()  # second cancel must be a no-op
    await kv.wait_closed()
    await kv.wait_closed()  # second wait must be a no-op


async def test_keepalive_start_idempotent_does_not_double_task(
    fast_sleep: None,
) -> None:
    del fast_sleep
    bot = AsyncMock()
    bot.send_chat_action = AsyncMock()
    kv = TypingKeepalive(bot=bot, chat_id=5)

    kv.start()
    first_task = kv._task  # noqa: SLF001 — test introspection of internal state
    kv.start()
    second_task = kv._task  # noqa: SLF001
    assert first_task is second_task

    kv.cancel()
    await kv.wait_closed()


# --------------------------------------------------------------------------- #
# _typing_loop — integration with the bus + DB.
# --------------------------------------------------------------------------- #


async def test_typing_loop_starts_keepalive_on_text_event(
    session_factory: async_sessionmaker[AsyncSession],
    fast_sleep: None,
) -> None:
    del fast_sleep
    await _seed_user(session_factory, tg_user_id=1, last_chat_id=1001, is_owner=True)

    bus = EventBus()
    bot = AsyncMock()
    bot.send_chat_action = AsyncMock()

    loop_task = asyncio.create_task(_typing_loop(bus, bot, session_factory))
    await asyncio.sleep(0)

    await bus.publish(
        "session.event",
        {"session_id": "sid", "seq": 0, "event": _make_text_event("ping")},
    )

    await _wait_for_call_count(bot.send_chat_action, 1)
    chat_ids = {call.args[0] for call in bot.send_chat_action.await_args_list}
    assert chat_ids == {1001}

    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task


async def test_typing_loop_cancels_keepalive_on_result_event(
    session_factory: async_sessionmaker[AsyncSession],
    fast_sleep: None,
) -> None:
    """A ``ResultEvent`` must cancel an in-flight keepalive for that chat."""
    del fast_sleep
    await _seed_user(session_factory, tg_user_id=1, last_chat_id=1001, is_owner=True)

    bus = EventBus()
    bot = AsyncMock()
    bot.send_chat_action = AsyncMock()

    loop_task = asyncio.create_task(_typing_loop(bus, bot, session_factory))
    await asyncio.sleep(0)

    # Start a keepalive via a non-result event.
    await bus.publish(
        "session.event",
        {"session_id": "sid", "seq": 0, "event": _make_text_event("ping")},
    )
    await _wait_for_call_count(bot.send_chat_action, 1)

    # Result event must tear down the keepalive.
    await bus.publish(
        "session.event",
        {
            "session_id": "sid",
            "seq": 1,
            "event": ResultEvent(type="result", subtype="success", duration_ms=2200),
        },
    )

    # Give the loop a moment to act.
    await asyncio.sleep(0.05)
    count_after_result = bot.send_chat_action.await_count

    # Wait further; no new send_chat_action calls should arrive after cancel.
    await asyncio.sleep(0.1)
    assert bot.send_chat_action.await_count == count_after_result

    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task


async def test_typing_loop_cleans_up_on_cancellation(
    session_factory: async_sessionmaker[AsyncSession],
    fast_sleep: None,
) -> None:
    """Cancelling the typing loop must cancel every per-chat keepalive."""
    del fast_sleep
    await _seed_user(session_factory, tg_user_id=1, last_chat_id=1001, is_owner=True)
    await _seed_user(session_factory, tg_user_id=2, last_chat_id=1002)

    bus = EventBus()
    bot = AsyncMock()
    bot.send_chat_action = AsyncMock()

    loop_task = asyncio.create_task(_typing_loop(bus, bot, session_factory))
    await asyncio.sleep(0)

    await bus.publish(
        "session.event",
        {"session_id": "sid", "seq": 0, "event": _make_text_event("ping")},
    )
    await _wait_for_call_count(bot.send_chat_action, 2)

    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task

    count_after_shutdown = bot.send_chat_action.await_count
    await asyncio.sleep(0.05)
    # No more chat actions arrive once the loop is torn down.
    assert bot.send_chat_action.await_count == count_after_shutdown
