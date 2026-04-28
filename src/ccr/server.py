"""Top-level async ``serve`` orchestration.

Polling-only for now (CCR-006). Uvicorn / web server wiring lands in
CCR-012; the bot polling task will then run alongside uvicorn under the
same asyncio loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ccr.bot.app import run_polling
from ccr.db.engine import AsyncSessionMaker, create_engine_from_settings

if TYPE_CHECKING:
    from ccr.config import Settings


log = structlog.get_logger(__name__)


async def serve(settings: Settings) -> None:
    """Run the bot dispatcher against a fresh DB engine."""
    engine = create_engine_from_settings(settings)
    db_factory = AsyncSessionMaker(engine)
    try:
        await run_polling(settings, db_factory)
    finally:
        await engine.dispose()


__all__ = ["serve"]
