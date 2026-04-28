"""Allowlist middleware for Telegram updates.

Injects an ``is_paired_user`` flag into aiogram's per-handler ``data`` dict
and short-circuits non-`/start` traffic from unpaired senders with a fixed
rejection string. Also stamps ``PairedUser.last_chat_id`` on every message
from a paired user so :func:`ccr.bot.notify.notify_owner` later knows where
to deliver owner DMs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import select

from ccr.auth.allowlist import is_paired
from ccr.db.models import PairedUser

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


_REJECTION = "Not paired. Send /start to request access."


def _extract_sender(event: TelegramObject) -> tuple[int | None, int | None, Message | None]:
    """Return ``(tg_user_id, chat_id, reply_target)`` for ``event``.

    ``reply_target`` is the :class:`Message` we can ``answer()`` if we need to
    short-circuit. For non-Message / non-CallbackQuery updates everything is
    ``None`` and the middleware falls through.
    """
    if isinstance(event, Message):
        if event.from_user is None:
            return (None, event.chat.id, event)
        return (event.from_user.id, event.chat.id, event)
    if isinstance(event, CallbackQuery):
        msg = event.message if isinstance(event.message, Message) else None
        chat_id = msg.chat.id if msg is not None else None
        sender_id = event.from_user.id if event.from_user is not None else None
        return (sender_id, chat_id, msg)
    return (None, None, None)


def _is_start_command(event: TelegramObject) -> bool:
    """Return ``True`` iff this is a ``/start`` (or ``/start@bot`` / ``/start <args>``).

    Telegram appends ``@botname`` in group chats; we accept that variant so the
    bootstrap path keeps working when the bot is added to a group.
    """
    if not isinstance(event, Message):
        return False
    text = event.text
    if text is None:
        return False
    return text == "/start" or text.startswith(("/start ", "/start@"))


class AllowlistMiddleware(BaseMiddleware):
    """Inject pairing state into handler ``data`` and gate unpaired traffic."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user_id, chat_id, reply_target = _extract_sender(event)

        if tg_user_id is None:
            return await handler(event, data)

        db_factory: async_sessionmaker[AsyncSession] = data["db_factory"]

        async with db_factory() as db:
            paired = await is_paired(db, tg_user_id)
            if paired and chat_id is not None:
                row = await db.scalar(
                    select(PairedUser).where(PairedUser.tg_user_id == tg_user_id),
                )
                if row is not None and row.last_chat_id != chat_id:
                    row.last_chat_id = chat_id
                    await db.commit()

        data["is_paired_user"] = paired

        if not paired and not _is_start_command(event):
            if reply_target is not None:
                await reply_target.answer(_REJECTION)
            return None

        return await handler(event, data)


__all__ = ["AllowlistMiddleware"]
