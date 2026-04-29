"""aiogram dispatcher assembly + polling entrypoint.

The dispatcher wires the allowlist middleware onto both message and
callback-query update streams, stashes the ``db_factory`` (and optional
``session_manager``) in workflow data so handlers can open their own
short-lived DB sessions and drive the Claude subprocess, and includes the
pairing + session routers. ``run_polling`` instantiates the :class:`Bot`
and enters ``dp.start_polling``; the bot token is read from settings and
never logged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from ccr.bot.handlers.pairing import router as pairing_router
from ccr.bot.handlers.permission import router as permission_router
from ccr.bot.handlers.session import router as session_router
from ccr.bot.middlewares import AllowlistMiddleware

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ccr.claude.manager import SessionManager
    from ccr.config import Settings


log = structlog.get_logger(__name__)


def build_dispatcher(
    settings: Settings,
    db_factory: async_sessionmaker[AsyncSession],
    session_manager: SessionManager | None = None,
) -> tuple[Bot, Dispatcher]:
    """Build the aiogram :class:`Bot` and :class:`Dispatcher` for polling."""
    dp = Dispatcher()
    dp["db_factory"] = db_factory
    if session_manager is not None:
        dp["session_manager"] = session_manager

    middleware = AllowlistMiddleware()
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)

    dp.include_router(pairing_router)
    dp.include_router(session_router)
    dp.include_router(permission_router)

    bot = Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    return bot, dp


async def run_polling(
    settings: Settings,
    db_factory: async_sessionmaker[AsyncSession],
    session_manager: SessionManager | None = None,
) -> None:
    """Start long-polling against the Telegram Bot API."""
    bot, dp = build_dispatcher(settings, db_factory, session_manager=session_manager)
    log.info("Bot started, awaiting updates")
    await dp.start_polling(bot)


__all__ = ["build_dispatcher", "run_polling"]
