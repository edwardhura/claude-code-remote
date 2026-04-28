"""Notification helpers — owner DM and broadcast to paired users.

Both helpers are tolerant of paired rows that have not yet messaged the bot
(``last_chat_id IS NULL``): they log and skip rather than raising. Per-chat
delivery failures during a broadcast are caught individually so a single
blocked user does not take down the rest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from ccr.db.models import PairedUser

if TYPE_CHECKING:
    from aiogram import Bot
    from sqlalchemy.ext.asyncio import AsyncSession


log = structlog.get_logger(__name__)


async def notify_owner(bot: Bot, db: AsyncSession, message: str) -> None:
    """Send ``message`` to the active owner's last known chat.

    No-op (with a warning log) if no owner is paired or the owner has not
    messaged the bot yet (``last_chat_id`` is ``NULL``).
    """
    stmt = select(PairedUser).where(
        PairedUser.is_owner.is_(True),
        PairedUser.revoked_at.is_(None),
    )
    owner: PairedUser | None = await db.scalar(stmt)
    if owner is None:
        log.warning("notify_owner_no_owner")
        return
    if owner.last_chat_id is None:
        log.warning("owner_last_chat_id_unknown", tg_user_id=owner.tg_user_id)
        return
    await bot.send_message(owner.last_chat_id, message, parse_mode="HTML")


async def broadcast_paired(
    bot: Bot,
    db: AsyncSession,
    message: str,
    exclude: set[int] | None = None,
) -> None:
    """Send ``message`` to every paired user with a known ``last_chat_id``.

    Skips ``tg_user_id`` values in ``exclude`` (defaulting to none). Telegram
    delivery errors are logged and swallowed per recipient so one blocked
    chat does not interrupt the broadcast.
    """
    excluded = exclude or set()
    stmt = select(PairedUser).where(
        PairedUser.revoked_at.is_(None),
        PairedUser.last_chat_id.is_not(None),
    )
    rows = (await db.execute(stmt)).scalars().all()
    for row in rows:
        if row.tg_user_id in excluded:
            continue
        if row.last_chat_id is None:
            continue
        try:
            await bot.send_message(row.last_chat_id, message, parse_mode="HTML")
        except TelegramAPIError as exc:
            log.warning(
                "broadcast_send_failed",
                tg_user_id=row.tg_user_id,
                chat_id=row.last_chat_id,
                error=str(exc),
            )


__all__ = ["broadcast_paired", "notify_owner"]
