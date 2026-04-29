"""Per-chat typing-indicator keepalive task.

While a Claude session is producing events, calls
``bot.send_chat_action(chat_id, "typing")`` every 4 seconds so the Telegram
client shows "typing…". One :class:`TypingKeepalive` instance per chat;
started on first non-result session event, cancelled on ``ResultEvent`` or
``manager.stop()``.

Telegram chat actions expire after ~5 s server-side; the 4 s interval keeps
a 1 s safety margin. ``TelegramAPIError`` is swallowed per-chat so a single
rate-limited chat never kills the indicator for everyone else.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import structlog
from aiogram.exceptions import TelegramAPIError

if TYPE_CHECKING:
    from aiogram import Bot

log = structlog.get_logger(__name__)

TYPING_INTERVAL = 4.0  # seconds


class TypingKeepalive:
    """Sends ``typing`` chat action every :data:`TYPING_INTERVAL` seconds.

    Lifecycle mirrors :class:`~ccr.server._ChatSender`:
    - :meth:`start` spawns the background task (idempotent).
    - :meth:`cancel` requests cancellation (idempotent, swallows errors).
    - :meth:`wait_closed` awaits the task; safe to call after cancel.
    """

    def __init__(self, *, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(),
                name=f"typing-keepalive-{self._chat_id}",
            )

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
            try:
                await self._bot.send_chat_action(self._chat_id, "typing")
            except TelegramAPIError as exc:
                log.warning(
                    "typing_keepalive.send_failed",
                    chat_id=self._chat_id,
                    error=str(exc),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "typing_keepalive.unexpected_error",
                    chat_id=self._chat_id,
                )
            await asyncio.sleep(TYPING_INTERVAL)


__all__ = ["TYPING_INTERVAL", "TypingKeepalive"]
