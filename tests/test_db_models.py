"""Tests for :mod:`ccr.db.models` and the partial-unique owner index."""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
import sqlalchemy as sa
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


REPO_ROOT = Path(__file__).resolve().parent.parent


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


async def test_session_round_trips_claude_session_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Round-trip a row both with the column set and with NULL."""
    set_id = uuid.uuid4()
    null_id = uuid.uuid4()
    async with get_session(session_factory) as session:
        session.add(
            Session(
                id=set_id,
                started_at=datetime.now(UTC),
                status="completed",
                claude_session_id="abc-123",
            )
        )
        session.add(
            Session(
                id=null_id,
                started_at=datetime.now(UTC),
                status="completed",
                claude_session_id=None,
            )
        )

    async with session_factory() as session:
        set_row = await session.scalar(select(Session).where(Session.id == set_id))
        null_row = await session.scalar(select(Session).where(Session.id == null_id))
    assert set_row is not None
    assert null_row is not None
    assert set_row.claude_session_id == "abc-123"
    assert null_row.claude_session_id is None


async def test_session_partial_unique_index_on_claude_session_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two non-NULL duplicates raise; two NULLs are fine."""
    async with get_session(session_factory) as session:
        session.add(
            Session(
                started_at=datetime.now(UTC),
                status="completed",
                claude_session_id="dup-id",
            )
        )

    with pytest.raises(IntegrityError):
        async with get_session(session_factory) as session:
            session.add(
                Session(
                    started_at=datetime.now(UTC),
                    status="completed",
                    claude_session_id="dup-id",
                )
            )

    async with get_session(session_factory) as session:
        session.add(
            Session(
                started_at=datetime.now(UTC),
                status="completed",
                claude_session_id=None,
            )
        )
        session.add(
            Session(
                started_at=datetime.now(UTC),
                status="completed",
                claude_session_id=None,
            )
        )


def test_alembic_round_trips_claude_session_id_migration(tmp_path: Path) -> None:
    """``alembic upgrade head && downgrade base && upgrade head`` against tmp DB."""
    db_path = tmp_path / "alembic.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

    # Materialise a temporary alembic.ini whose ``sqlalchemy.url`` points at
    # the tmp DB. Alembic reads the URL from the .ini at config-load time,
    # so a -x override would not propagate cleanly through env.py.
    override_ini = tmp_path / "alembic.ini"
    original_ini = (REPO_ROOT / "alembic.ini").read_text(encoding="utf-8")
    override_ini.write_text(
        original_ini.replace(
            "sqlalchemy.url = sqlite+aiosqlite:///data/ccr.db",
            f"sqlalchemy.url = {url}",
        ),
        encoding="utf-8",
    )

    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "TELEGRAM_BOT_TOKEN": "x",
        "PUBLIC_URL": "http://localhost",
        "JWT_SECRET": "x" * 32,
        "DATA_DIR": str(tmp_path),
    }
    base_cmd = [
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(override_ini),
    ]

    def _run(*args: str) -> None:
        subprocess.run(
            [*base_cmd, *args],
            cwd=str(REPO_ROOT),
            env=env,
            check=True,
            capture_output=True,
        )

    sync_url = f"sqlite:///{db_path.as_posix()}"

    def _has_column(name: str) -> bool:
        engine = sa.create_engine(sync_url)
        try:
            inspector = sa.inspect(engine)
            cols = {c["name"] for c in inspector.get_columns("sessions")}
            return name in cols
        finally:
            engine.dispose()

    def _has_table(name: str) -> bool:
        engine = sa.create_engine(sync_url)
        try:
            inspector = sa.inspect(engine)
            return name in inspector.get_table_names()
        finally:
            engine.dispose()

    _run("upgrade", "head")
    assert _has_column("claude_session_id")

    _run("downgrade", "base")
    # After downgrade base the entire schema is gone.
    assert not _has_table("sessions")

    _run("upgrade", "head")
    assert _has_column("claude_session_id")
