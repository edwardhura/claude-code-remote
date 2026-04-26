"""Tests for :mod:`ccr.db.models` and the partial-unique owner index."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ccr.db.engine import AsyncSessionMaker, get_session
from ccr.db.models import Base, PairedUser, PairingCode, Session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine


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


async def test_insert_and_query_paired_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with get_session(session_factory) as session:
        user = PairedUser(
            tg_user_id=111,
            tg_username="alice",
            is_owner=True,
            approved_at=datetime.now(UTC),
        )
        session.add(user)

    async with session_factory() as session:
        result = await session.execute(select(PairedUser).where(PairedUser.tg_user_id == 111))
        row = result.scalar_one()
        assert row.tg_username == "alice"
        assert row.is_owner is True
        assert isinstance(row.id, uuid.UUID)


async def test_partial_unique_index_prevents_two_owners(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with get_session(session_factory) as session:
        session.add(
            PairedUser(
                tg_user_id=111,
                is_owner=True,
                approved_at=datetime.now(UTC),
            )
        )

    with pytest.raises(IntegrityError):
        async with get_session(session_factory) as session:
            session.add(
                PairedUser(
                    tg_user_id=222,
                    is_owner=True,
                    approved_at=datetime.now(UTC),
                )
            )


async def test_partial_unique_index_allows_many_non_owners(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with get_session(session_factory) as session:
        session.add(
            PairedUser(
                tg_user_id=111,
                is_owner=True,
                approved_at=datetime.now(UTC),
            )
        )
        session.add(
            PairedUser(
                tg_user_id=222,
                is_owner=False,
                approved_at=datetime.now(UTC),
            )
        )
        session.add(
            PairedUser(
                tg_user_id=333,
                is_owner=False,
                approved_at=datetime.now(UTC),
            )
        )

    async with session_factory() as session:
        result = await session.execute(select(PairedUser))
        rows = list(result.scalars())
        assert len(rows) == 3
        owners = [r for r in rows if r.is_owner]
        assert len(owners) == 1


async def test_unique_tg_user_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with get_session(session_factory) as session:
        session.add(
            PairedUser(
                tg_user_id=999,
                approved_at=datetime.now(UTC),
            )
        )

    with pytest.raises(IntegrityError):
        async with get_session(session_factory) as session:
            session.add(
                PairedUser(
                    tg_user_id=999,
                    approved_at=datetime.now(UTC),
                )
            )


async def test_pairing_code_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with get_session(session_factory) as session:
        session.add(
            PairingCode(
                code="ABCD1234",
                tg_user_id=42,
                tg_username="bob",
                created_at=now,
                expires_at=now + timedelta(minutes=15),
            )
        )

    async with session_factory() as session:
        result = await session.execute(select(PairingCode).where(PairingCode.code == "ABCD1234"))
        row = result.scalar_one()
        assert row.tg_user_id == 42
        assert row.used_at is None


async def test_pairing_code_unique_code(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with get_session(session_factory) as session:
        session.add(
            PairingCode(
                code="DUPLICAT",
                tg_user_id=1,
                created_at=now,
                expires_at=now + timedelta(minutes=15),
            )
        )

    with pytest.raises(IntegrityError):
        async with get_session(session_factory) as session:
            session.add(
                PairingCode(
                    code="DUPLICAT",
                    tg_user_id=2,
                    created_at=now,
                    expires_at=now + timedelta(minutes=15),
                )
            )


async def test_session_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with get_session(session_factory) as session:
        session.add(
            Session(
                started_at=datetime.now(UTC),
                status="running",
                started_by_tg_user_id=42,
                first_prompt="hello",
            )
        )

    async with session_factory() as session:
        result = await session.execute(select(Session))
        row = result.scalar_one()
        assert row.status == "running"
        assert row.started_by_tg_user_id == 42
        assert row.first_prompt == "hello"
