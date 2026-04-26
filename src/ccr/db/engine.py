"""Async SQLAlchemy engine + session factory bound to the configured DB URL."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ccr.config import Settings


def _build_database_url(settings: Settings) -> str:
    """Compute the SQLAlchemy URL for the local SQLite database.

    Uses the ``data_dir`` from settings so tests / alternative deployments can
    redirect storage. The DB lives at ``<DATA_DIR>/ccr.db``.
    """
    db_path = settings.data_dir / "ccr.db"
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


def create_engine_from_settings(settings: Settings) -> AsyncEngine:
    """Build an :class:`AsyncEngine` bound to ``<DATA_DIR>/ccr.db``."""
    return create_async_engine(_build_database_url(settings), future=True)


def AsyncSessionMaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:  # noqa: N802
    """Return an async session factory bound to ``engine``.

    Capitalised by intent: ``AsyncSessionMaker`` reads as a class-style
    factory at call sites (matching SQLAlchemy's own ``async_sessionmaker``).
    """
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def get_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession`, committing on success and rolling back on error."""
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


__all__ = [
    "AsyncSessionMaker",
    "create_engine_from_settings",
    "get_session",
]
