"""Top-level async ``serve`` orchestration.

Wires together the bot polling task, the global :class:`SessionManager`,
and the Telegram broadcast loop that fans Claude session events out to all
paired users with a known ``last_chat_id``. Uvicorn / web server lands in
CCR-012 and will join the same ``asyncio.gather``.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from ccr.bot.app import run_polling
from ccr.bot.formatting import event_to_messages
from ccr.bot.typing import TypingKeepalive
from ccr.claude.events import ResultEvent
from ccr.claude.manager import SessionManager
from ccr.db.engine import AsyncSessionMaker, create_engine_from_settings
from ccr.db.models import PairedUser
from ccr.events import EventBus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ccr.config import Settings


log = structlog.get_logger(__name__)


async def serve(settings: Settings) -> None:
    """Run the bot dispatcher, session manager, and Telegram broadcast loop."""
    engine = create_engine_from_settings(settings)
    db_factory = AsyncSessionMaker(engine)
    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=db_factory, settings=settings)

    broadcast_bot = Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    broadcast_task = asyncio.create_task(
        _broadcast_loop(bus, broadcast_bot, db_factory),
        name="broadcast",
    )
    typing_task = asyncio.create_task(
        _typing_loop(bus, broadcast_bot, db_factory),
        name="typing",
    )

    try:
        await asyncio.gather(
            run_polling(settings, db_factory, session_manager=manager),
            broadcast_task,
            typing_task,
        )
    finally:
        broadcast_task.cancel()
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await broadcast_task
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task
        with contextlib.suppress(Exception):
            await broadcast_bot.session.close()
        await engine.dispose()


async def _broadcast_loop(
    bus: EventBus,
    bot: Bot,
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fan ``session.event`` payloads out to every paired user.

    For each formatted message the loop enqueues onto a per-chat
    :class:`asyncio.Queue` so a slow Telegram chat never blocks delivery to
    the others. One persistent sender task per chat drains its queue. Both
    the queues and the sender tasks live for the lifetime of the broadcast
    loop; cancellation of the loop cascades to every sender.
    """
    senders: dict[int, _ChatSender] = {}
    try:
        async for payload in bus.subscribe("session.event"):
            if not isinstance(payload, dict):
                continue
            event = payload.get("event")
            if event is None:
                continue
            messages = event_to_messages(event)
            if not messages:
                continue
            chat_ids = await _list_active_chat_ids(db_factory)
            for chat_id in chat_ids:
                sender = senders.get(chat_id)
                if sender is None:
                    sender = _ChatSender(bot=bot, chat_id=chat_id)
                    sender.start()
                    senders[chat_id] = sender
                for message in messages:
                    sender.enqueue(message)
    finally:
        for sender in senders.values():
            sender.cancel()
        for sender in senders.values():
            await sender.wait_closed()


async def _typing_loop(
    bus: EventBus,
    bot: Bot,
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Drive a per-chat ``typing…`` indicator while a session emits events.

    Subscribes to ``session.event`` independently from
    :func:`_broadcast_loop`. On every non-result event a
    :class:`~ccr.bot.typing.TypingKeepalive` is created (or refreshed) for
    every active paired chat; on a :class:`ResultEvent` the keepalives are
    cancelled and disposed. Cancellation of the loop cascades to every
    keepalive task.
    """
    keepalives: dict[int, TypingKeepalive] = {}
    try:
        async for payload in bus.subscribe("session.event"):
            if not isinstance(payload, dict):
                continue
            event = payload.get("event")
            if event is None:
                continue
            chat_ids = await _list_active_chat_ids(db_factory)
            is_result = isinstance(event, ResultEvent)
            for chat_id in chat_ids:
                if is_result:
                    kv = keepalives.pop(chat_id, None)
                    if kv is not None:
                        kv.cancel()
                        await kv.wait_closed()
                else:
                    kv = keepalives.get(chat_id)
                    if kv is None:
                        kv = TypingKeepalive(bot=bot, chat_id=chat_id)
                        keepalives[chat_id] = kv
                    kv.start()
    finally:
        for kv in keepalives.values():
            kv.cancel()
        for kv in keepalives.values():
            await kv.wait_closed()


async def _list_active_chat_ids(
    db_factory: async_sessionmaker[AsyncSession],
) -> list[int]:
    """Return the ``last_chat_id`` for every active paired user."""
    async with db_factory() as db:
        stmt = select(PairedUser.last_chat_id).where(
            PairedUser.revoked_at.is_(None),
            PairedUser.last_chat_id.is_not(None),
        )
        result = await db.execute(stmt)
        return [row for row in result.scalars().all() if row is not None]


class _ChatSender:
    """Per-chat queue + sender task.

    Decouples broadcast fan-out from Telegram delivery so a single slow chat
    cannot back-pressure the publisher. Errors from
    :meth:`Bot.send_message` are logged and swallowed — broadcast is best-
    effort.
    """

    def __init__(self, *, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name=f"broadcast-sender-{self._chat_id}",
            )

    def enqueue(self, message: str) -> None:
        self._queue.put_nowait(message)

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def wait_closed(self) -> None:
        if self._task is None:
            return
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task

    async def _run(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                await self._bot.send_message(
                    self._chat_id,
                    message,
                    parse_mode="HTML",
                )
            except TelegramAPIError as exc:
                log.warning(
                    "broadcast_send_failed",
                    chat_id=self._chat_id,
                    error=str(exc),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "broadcast_send_unexpected_error",
                    chat_id=self._chat_id,
                )


__all__ = ["serve"]
