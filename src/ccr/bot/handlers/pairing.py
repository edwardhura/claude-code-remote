"""`/start` handler — three branches: already paired, bootstrap, normal.

The reply strings here are load-bearing: tests (and the README walkthrough)
match them exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Bot, Router
from aiogram.filters import Command

from ccr.auth import pairing
from ccr.bot.notify import notify_owner

if TYPE_CHECKING:
    from aiogram.types import Message
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


router = Router(name="pairing")


@router.message(Command("start"))
async def cmd_start(
    msg: Message,
    db_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    is_paired_user: bool,  # noqa: FBT001 — aiogram passes workflow data by name, not positionally
) -> None:
    """Handle ``/start`` per the three documented branches."""
    if msg.from_user is None:
        return

    if is_paired_user:
        await msg.answer("Paired. Send a prompt to start, or /new for a fresh session.")
        return

    async with db_factory() as db:
        code = await pairing.create_code(db, msg.from_user.id, msg.from_user.username)
        await db.commit()
        owner = await pairing.get_owner(db)

        if owner is None:
            await msg.answer(
                f"Bootstrap pairing.\nTelegram ID: <code>{msg.from_user.id}</code>\n"
                f"Code: <code>{code.code}</code>\n\n"
                f"On the project host:\n<code>python -m ccr pair approve {code.code}</code>",
                parse_mode="HTML",
            )
            return

        await msg.answer("Access requested. The owner has been notified.")
        username = msg.from_user.username or "?"
        await notify_owner(
            bot,
            db,
            f"Pairing request from @{username} "
            f"(id <code>{msg.from_user.id}</code>).\n"
            f"Approve with: <code>python -m ccr pair approve {code.code}</code>",
        )


__all__ = ["cmd_start", "router"]
