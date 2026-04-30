"""Tests for :meth:`SessionManager.reconcile_orphans`.

On bot crash / kill, the child Claude subprocess dies with the parent but the
:class:`Session` row stays ``status='running'`` forever because
``_db_finalize_session`` never runs. ``reconcile_orphans`` is invoked once at
``serve()`` startup to flip stale ``running`` rows to ``crashed`` so the DB
stops lying about live sessions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ccr.claude import SessionManager
from ccr.config import Settings
from ccr.db.engine import AsyncSessionMaker
from ccr.db.models import Base, Session
from ccr.events import EventBus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ccr.db'}",
        future=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return AsyncSessionMaker(engine)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="dummy-token",  # type: ignore[arg-type]
        public_url="http://localhost",  # type: ignore[arg-type]
        jwt_secret="x" * 32,  # type: ignore[arg-type]
        data_dir=tmp_path,
        claude_bin="/bin/true",
        subprocess_grace_kill_seconds=2,
    )


async def _seed_session(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    status: str,
    started_at: datetime | None = None,
) -> None:
    async with session_factory() as db:
        db.add(
            Session(
                id=session_id,
                started_at=started_at or datetime.now(UTC),
                status=status,
                started_by_tg_user_id=42,
                first_prompt="hi",
            ),
        )
        await db.commit()


async def test_reconcile_orphans_marks_running_rows_as_crashed(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    await _seed_session(session_factory, session_id=sid, status="running")

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    count = await manager.reconcile_orphans()
    assert count == 1

    async with session_factory() as db:
        row = await db.scalar(select(Session).where(Session.id == sid))
    assert row is not None
    assert row.status == "crashed"
    assert row.exit_reason == "bot restart"
    assert row.ended_at is not None


async def test_reconcile_orphans_returns_count(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_session(
        session_factory,
        session_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        status="running",
    )
    await _seed_session(
        session_factory,
        session_id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
        status="running",
    )

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    count = await manager.reconcile_orphans()
    assert count == 2


async def test_reconcile_orphans_idempotent(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sid = uuid.UUID("44444444-4444-4444-4444-444444444444")
    await _seed_session(session_factory, session_id=sid, status="running")

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    first = await manager.reconcile_orphans()
    second = await manager.reconcile_orphans()
    assert first == 1
    assert second == 0

    async with session_factory() as db:
        row = await db.scalar(select(Session).where(Session.id == sid))
    assert row is not None
    assert row.status == "crashed"
    assert row.exit_reason == "bot restart"


async def test_reconcile_orphans_does_not_touch_non_running_rows(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sid = uuid.UUID("55555555-5555-5555-5555-555555555555")
    await _seed_session(session_factory, session_id=sid, status="completed")

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    count = await manager.reconcile_orphans()
    assert count == 0

    async with session_factory() as db:
        row = await db.scalar(select(Session).where(Session.id == sid))
    assert row is not None
    assert row.status == "completed"
    assert row.exit_reason is None
    assert row.ended_at is None
