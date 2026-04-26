"""Lifecycle tests for :mod:`ccr.auth.pairing` and the ``ccr pair`` CLI.

Covers the full lifecycle described in CCR-004:

* code creation (collision retry)
* first approval auto-promotes to owner
* second approval becomes a regular paired user with ``approved_by`` set
* revoke owner refused, revoke non-owner OK
* expired and reused codes rejected
* invite happy path
* CLI subcommands produce the exact output strings the acceptance criteria pin
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
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

from ccr.auth.allowlist import is_owner, is_paired
from ccr.auth.pairing import (
    CannotRevokeOwnerError,
    PairingError,
    approve,
    create_code,
    get_owner,
    invite,
    list_paired,
    list_pending,
    revoke,
)
from ccr.cli import main
from ccr.db.engine import AsyncSessionMaker, get_session
from ccr.db.models import Base, PairingCode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine


REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Fixtures (mirrors tests/test_db_models.py from CCR-003).
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Pure-function lifecycle.
# --------------------------------------------------------------------------- #


async def test_create_code_persists_and_returns_8_char_uppercase_hex(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        pc = await create_code(session, tg_user_id=42, tg_username="alice")
        await session.commit()
    assert len(pc.code) == 8
    assert pc.code == pc.code.upper()
    assert int(pc.code, 16) >= 0
    assert pc.tg_user_id == 42
    assert pc.tg_username == "alice"
    assert pc.used_at is None


async def test_create_code_retries_on_collision(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pre-seed an existing code so the first generated value will collide.
    async with get_session(session_factory) as session:
        session.add(
            PairingCode(
                code="DEADBEEF",
                tg_user_id=1,
                created_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
        )

    candidates = iter(["DEADBEEF", "FEEDFACE"])
    monkeypatch.setattr(
        "ccr.auth.pairing._generate_code",
        lambda: next(candidates),
    )

    async with session_factory() as session:
        pc = await create_code(session, tg_user_id=2, tg_username=None)
        await session.commit()
    assert pc.code == "FEEDFACE"


async def test_create_code_raises_after_5_collisions(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with get_session(session_factory) as session:
        session.add(
            PairingCode(
                code="DEADBEEF",
                tg_user_id=1,
                created_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
        )

    monkeypatch.setattr("ccr.auth.pairing._generate_code", lambda: "DEADBEEF")

    with pytest.raises(PairingError):
        async with session_factory() as session:
            await create_code(session, tg_user_id=2, tg_username=None)


async def test_first_approval_auto_promotes_to_owner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        pc = await create_code(session, tg_user_id=111, tg_username="alice")
        await session.commit()

    async with session_factory() as session:
        user = await approve(session, pc.code)

    assert user.is_owner is True
    assert user.approved_by_tg_user_id is None
    assert user.tg_user_id == 111
    assert user.tg_username == "alice"

    async with session_factory() as session:
        owner = await get_owner(session)
        assert owner is not None
        assert owner.tg_user_id == 111
        assert await is_owner(session, 111) is True
        assert await is_paired(session, 111) is True


async def test_second_approval_is_non_owner_and_links_to_owner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        first = await create_code(session, tg_user_id=111, tg_username="alice")
        await session.commit()
    async with session_factory() as session:
        owner = await approve(session, first.code)
    assert owner.is_owner is True

    async with session_factory() as session:
        second = await create_code(session, tg_user_id=222, tg_username="bob")
        await session.commit()
    async with session_factory() as session:
        friend = await approve(session, second.code)

    assert friend.is_owner is False
    assert friend.approved_by_tg_user_id == 111
    assert friend.tg_user_id == 222

    async with session_factory() as session:
        assert await is_paired(session, 222) is True
        assert await is_owner(session, 222) is False


async def test_approve_marks_code_used(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        pc = await create_code(session, tg_user_id=111, tg_username="alice")
        await session.commit()
    async with session_factory() as session:
        await approve(session, pc.code)
    async with session_factory() as session:
        reloaded = await session.scalar(select(PairingCode).where(PairingCode.code == pc.code))
        assert reloaded is not None
        assert reloaded.used_at is not None


async def test_reused_code_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        pc = await create_code(session, tg_user_id=111, tg_username="alice")
        await session.commit()
    async with session_factory() as session:
        await approve(session, pc.code)

    with pytest.raises(PairingError):
        async with session_factory() as session:
            await approve(session, pc.code)


async def test_expired_code_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    issued_at = datetime(2026, 1, 1, tzinfo=UTC)
    async with session_factory() as session:
        await create_code(
            session,
            tg_user_id=111,
            tg_username="alice",
            ttl_seconds=900,
            now=issued_at,
        )
        await session.commit()

    later = issued_at + timedelta(seconds=901)
    async with session_factory() as session:
        codes = await session.execute(select(PairingCode.code))
        code = codes.scalar_one()
        with pytest.raises(PairingError):
            await approve(session, code, now=later)


async def test_unknown_code_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(PairingError):
        async with session_factory() as session:
            await approve(session, "NOPECODE")


async def test_revoke_non_owner_succeeds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        first = await create_code(session, tg_user_id=111, tg_username="alice")
        await session.commit()
    async with session_factory() as session:
        await approve(session, first.code)
    async with session_factory() as session:
        second = await create_code(session, tg_user_id=222, tg_username="bob")
        await session.commit()
    async with session_factory() as session:
        await approve(session, second.code)

    async with session_factory() as session:
        await revoke(session, 222)

    async with session_factory() as session:
        assert await is_paired(session, 222) is False
        assert await is_paired(session, 111) is True


async def test_revoke_owner_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        pc = await create_code(session, tg_user_id=111, tg_username="alice")
        await session.commit()
    async with session_factory() as session:
        await approve(session, pc.code)

    with pytest.raises(CannotRevokeOwnerError):
        async with session_factory() as session:
            await revoke(session, 111)

    async with session_factory() as session:
        assert await is_owner(session, 111) is True


async def test_revoke_unknown_user_raises_pairing_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(PairingError):
        async with session_factory() as session:
            await revoke(session, 999_999)


async def test_invite_first_promotes_to_owner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        user = await invite(session, tg_user_id=42, label="me")

    assert user.is_owner is True
    assert user.label == "me"
    assert user.approved_by_tg_user_id is None


async def test_invite_subsequent_pre_approves_friend(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await invite(session, tg_user_id=42, label="me")

    async with session_factory() as session:
        friend = await invite(session, tg_user_id=99, label="friend")

    assert friend.is_owner is False
    assert friend.label == "friend"
    assert friend.approved_by_tg_user_id == 42


async def test_invite_existing_clears_revocation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await invite(session, tg_user_id=42, label="me")
    async with session_factory() as session:
        friend = await invite(session, tg_user_id=99, label="friend")
        assert friend.revoked_at is None

    async with session_factory() as session:
        await revoke(session, 99)
    async with session_factory() as session:
        re_added = await invite(session, tg_user_id=99, label=None)
        assert re_added.revoked_at is None
        assert re_added.is_owner is False


async def test_list_pending_filters_used_and_expired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    issued = datetime(2026, 1, 1, tzinfo=UTC)
    async with session_factory() as session:
        active = await create_code(
            session,
            tg_user_id=111,
            tg_username="alice",
            ttl_seconds=3600,
            now=issued,
        )
        await create_code(
            session,
            tg_user_id=222,
            tg_username="bob",
            ttl_seconds=60,
            now=issued,
        )
        await session.commit()

    much_later = issued + timedelta(seconds=120)
    async with session_factory() as session:
        rows = await list_pending(session, now=much_later)
        codes = [r.code for r in rows]
        assert active.code in codes
        assert len(codes) == 1


async def test_list_paired_returns_all_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await invite(session, tg_user_id=42, label="me")
    async with session_factory() as session:
        await invite(session, tg_user_id=99, label="friend")
    async with session_factory() as session:
        await revoke(session, 99)

    async with session_factory() as session:
        rows = await list_paired(session)
        ids = {r.tg_user_id for r in rows}
        assert ids == {42, 99}


# --------------------------------------------------------------------------- #
# CLI subcommand tests — drive the real CLI in-process against a temp SQLite DB.
# --------------------------------------------------------------------------- #


@pytest.fixture
def cli_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Configure env + initialised `data/ccr.db` on disk for in-process CLI calls.

    Settings env vars are set via monkeypatch (auto-reverted on test teardown);
    cwd is pinned to the repo root so ``ccr init-db`` finds ``alembic.ini``.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUBLIC_URL", "https://example.invalid")
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(REPO_ROOT)
    main(["init-db"])
    return tmp_path


def _seed_pairing_code(workspace: Path, code: str, tg_user_id: int) -> None:
    """Hand-insert a `pairing_codes` row using the CLI's same DB file.

    SQLAlchemy's ``Uuid(as_uuid=True)`` stores UUIDs on SQLite as a 32-char
    hex string (no dashes). The seed must use the same form so the row's
    primary key round-trips through the ORM during ``pair approve``.
    """
    db_path = workspace / "data" / "ccr.db"
    now = datetime.now(UTC).isoformat()
    expires = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO pairing_codes "
            "(id, code, tg_user_id, tg_username, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, code, tg_user_id, None, now, expires),
        )
        conn.commit()


@pytest.mark.usefixtures("cli_workspace")
def test_cli_pair_list_empty_db(capsys: pytest.CaptureFixture[str]) -> None:
    main(["pair", "list"])
    assert capsys.readouterr().out.strip() == "(empty)"


@pytest.mark.usefixtures("cli_workspace")
def test_cli_pair_pending_empty_db(capsys: pytest.CaptureFixture[str]) -> None:
    main(["pair", "pending"])
    assert capsys.readouterr().out.strip() == "(empty)"


def test_cli_pair_pending_shows_seeded_codes(
    cli_workspace: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_pairing_code(cli_workspace, code="PENDING0", tg_user_id=555)
    main(["pair", "pending"])
    out = capsys.readouterr().out
    assert "PENDING0" in out
    assert "555" in out


@pytest.mark.usefixtures("cli_workspace")
def test_cli_pair_approve_invalid_code_exits_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["pair", "approve", "NOPECODE"])
    assert exc_info.value.code == 1
    assert "Code invalid or expired" in capsys.readouterr().err


@pytest.mark.usefixtures("cli_workspace")
def test_cli_pair_revoke_unknown_user_exits_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["pair", "revoke", "99999999"])
    assert exc_info.value.code == 1
    assert "No paired user" in capsys.readouterr().err


@pytest.mark.usefixtures("cli_workspace")
def test_cli_pair_invite_first_promotes_to_owner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["pair", "invite", "42", "--label", "me"])
    assert capsys.readouterr().out.strip() == "Invited Telegram user 42 (owner)"


@pytest.mark.usefixtures("cli_workspace")
def test_cli_pair_invite_subsequent_friend(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["pair", "invite", "42"])
    capsys.readouterr()
    main(["pair", "invite", "99", "--label", "friend"])
    assert capsys.readouterr().out.strip() == "Invited Telegram user 99 (paired)"


def test_cli_full_lifecycle(
    cli_workspace: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # First approval becomes owner.
    _seed_pairing_code(cli_workspace, code="OWNER001", tg_user_id=123_456_789)
    main(["pair", "approve", "OWNER001"])
    assert capsys.readouterr().out.strip() == "Approved Telegram user 123456789 (owner)"

    main(["pair", "list"])
    pair_list_out = capsys.readouterr().out
    assert "123456789" in pair_list_out
    assert "owner" in pair_list_out

    # Second approval becomes a paired (non-owner) user.
    _seed_pairing_code(cli_workspace, code="FRIEND01", tg_user_id=987_654_321)
    main(["pair", "approve", "FRIEND01"])
    assert capsys.readouterr().out.strip() == "Approved Telegram user 987654321 (paired)"

    # Revoke owner is refused.
    with pytest.raises(SystemExit) as exc_info:
        main(["pair", "revoke", "123456789"])
    assert exc_info.value.code == 1
    assert capsys.readouterr().err.strip() == "Cannot revoke owner."

    # Revoke friend succeeds and pair list no longer shows them as active.
    main(["pair", "revoke", "987654321"])
    assert "Revoked Telegram user 987654321" in capsys.readouterr().out

    main(["pair", "list"])
    pair_list_after = capsys.readouterr().out
    # Still listed (soft-revoke) but flagged "revoked", not "paired"/active.
    rows = [line for line in pair_list_after.splitlines() if "987654321" in line]
    assert len(rows) == 1
    assert "revoked" in rows[0]
    assert "paired" not in rows[0].split()


def test_paired_users_table_after_lifecycle(cli_workspace: Path) -> None:
    """Sanity-check that the CLI lifecycle landed the right rows in the DB."""
    _seed_pairing_code(cli_workspace, code="OWNER002", tg_user_id=100)
    main(["pair", "approve", "OWNER002"])
    _seed_pairing_code(cli_workspace, code="FRIEND02", tg_user_id=200)
    main(["pair", "approve", "FRIEND02"])
    main(["pair", "revoke", "200"])

    db_path = cli_workspace / "data" / "ccr.db"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT tg_user_id, is_owner, approved_by_tg_user_id, revoked_at FROM paired_users",
        ).fetchall()
    by_id = {row[0]: row for row in rows}
    assert by_id[100][1] == 1
    assert by_id[100][2] is None
    assert by_id[200][1] == 0
    assert by_id[200][2] == 100
    assert by_id[200][3] is not None
