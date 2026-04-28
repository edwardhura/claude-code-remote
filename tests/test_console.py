"""Tests for :mod:`ccr.console.app`.

The REPL's ``run(..., once=...)`` path skips prompt_toolkit's input
machinery entirely, so the bulk of these tests just call ``run`` directly
with a hand-crafted ``Settings`` and capture stdout via pytest's
``capsys`` fixture. The DB is a real on-disk SQLite under ``tmp_path``;
the schema is created via ``Base.metadata.create_all`` instead of
``alembic upgrade head`` to keep tests fast and self-contained.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from ccr.cli import main
from ccr.config import Settings
from ccr.console.app import COMMANDS, build_completer, dispatch, run
from ccr.db.models import Base

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


# --------------------------------------------------------------------------- #
# Fixtures.
# --------------------------------------------------------------------------- #


def _make_settings(tmp_path: Path) -> Settings:
    """Build a fully-configured Settings pointing at ``tmp_path/data``."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        telegram_bot_token="test-token",  # type: ignore[arg-type]
        public_url="http://127.0.0.1:1/",  # type: ignore[arg-type]
        jwt_secret="x" * 64,  # type: ignore[arg-type]
        data_dir=data_dir,
    )


async def _create_schema(settings: Settings) -> None:
    """Create the SQLAlchemy schema directly (no Alembic) at ``data_dir/ccr.db``."""
    db_path = settings.data_dir / "ccr.db"
    engine: AsyncEngine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        future=True,
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = _make_settings(tmp_path)
    asyncio.run(_create_schema(s))
    return s


def _seed_pairing_code(settings: Settings, code: str, tg_user_id: int) -> None:
    """Hand-insert a `pairing_codes` row using sqlite3 directly.

    SQLAlchemy's ``Uuid(as_uuid=True)`` stores UUIDs as 32-char hex on SQLite.
    """
    db_path = settings.data_dir / "ccr.db"
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


def _seed_paired_user(settings: Settings, tg_user_id: int, *, is_owner: bool = False) -> None:
    """Hand-insert a `paired_users` row using sqlite3 directly."""
    db_path = settings.data_dir / "ccr.db"
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO paired_users "
            "(id, tg_user_id, tg_username, label, is_owner, "
            " approved_by_tg_user_id, approved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                tg_user_id,
                "alice",
                None,
                1 if is_owner else 0,
                None,
                now,
            ),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# `pair list` paths.
# --------------------------------------------------------------------------- #


def test_pair_list_empty(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    asyncio.run(run(settings, once="pair list"))
    out = capsys.readouterr().out
    assert out == "(empty)\n"


def test_pair_list_with_row(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_paired_user(settings, tg_user_id=42, is_owner=True)
    asyncio.run(run(settings, once="pair list"))
    out = capsys.readouterr().out
    assert "tg_user_id" in out
    assert "username" in out
    assert "42" in out
    assert "owner" in out


# --------------------------------------------------------------------------- #
# `pair pending`.
# --------------------------------------------------------------------------- #


def test_pair_pending_empty(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    asyncio.run(run(settings, once="pair pending"))
    assert capsys.readouterr().out == "(empty)\n"


def test_pair_pending_with_row(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_pairing_code(settings, code="ABCD0001", tg_user_id=555)
    asyncio.run(run(settings, once="pair pending"))
    out = capsys.readouterr().out
    assert "ABCD0001" in out
    assert "555" in out


# --------------------------------------------------------------------------- #
# Unknown command and help.
# --------------------------------------------------------------------------- #


def test_unknown_command(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    asyncio.run(run(settings, once="totally_unknown"))
    out = capsys.readouterr().out
    assert "Unknown command. Type 'help'." in out


def test_help_lists_all_commands(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(run(settings, once="help"))
    out = capsys.readouterr().out
    for fragment in (
        "pair list",
        "pair pending",
        "pair approve",
        "pair revoke",
        "pair invite",
        "status",
        "help",
        "exit",
    ):
        assert fragment in out


# --------------------------------------------------------------------------- #
# `pair approve`.
# --------------------------------------------------------------------------- #


def test_pair_approve_bad_code(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(run(settings, once="pair approve BADBADBA"))
    out = capsys.readouterr().out
    assert "Code invalid or expired" in out


def test_pair_approve_happy_path(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_pairing_code(settings, code="HAPPY001", tg_user_id=123_456_789)
    asyncio.run(run(settings, once="pair approve HAPPY001"))
    out = capsys.readouterr().out
    assert "Approved Telegram user 123456789" in out
    assert "(owner)" in out  # First approval auto-promotes to owner.


def test_pair_approve_second_user_is_paired(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_pairing_code(settings, code="OWNER001", tg_user_id=111)
    asyncio.run(run(settings, once="pair approve OWNER001"))
    capsys.readouterr()
    _seed_pairing_code(settings, code="FRIEND01", tg_user_id=222)
    asyncio.run(run(settings, once="pair approve FRIEND01"))
    out = capsys.readouterr().out
    assert "Approved Telegram user 222" in out
    assert "(paired)" in out


def test_pair_approve_missing_code_arg(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(run(settings, once="pair approve"))
    assert "Usage:" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# `pair revoke`.
# --------------------------------------------------------------------------- #


def test_pair_revoke_unknown_user(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(run(settings, once="pair revoke 999999"))
    out = capsys.readouterr().out
    assert "No paired user" in out


def test_pair_revoke_owner_refused(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_paired_user(settings, tg_user_id=42, is_owner=True)
    asyncio.run(run(settings, once="pair revoke 42"))
    out = capsys.readouterr().out
    assert "Cannot revoke owner." in out


def test_pair_revoke_invalid_id(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(run(settings, once="pair revoke not-a-number"))
    assert "Invalid tg_user_id" in capsys.readouterr().out


def test_pair_revoke_missing_arg(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(run(settings, once="pair revoke"))
    assert "Usage:" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# `pair invite`.
# --------------------------------------------------------------------------- #


def test_pair_invite_first_promotes_owner(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(run(settings, once="pair invite 42 me"))
    out = capsys.readouterr().out
    assert "Invited Telegram user 42 (owner)" in out


def test_pair_invite_subsequent_friend(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(run(settings, once="pair invite 42 me"))
    capsys.readouterr()
    asyncio.run(run(settings, once="pair invite 99 friend"))
    out = capsys.readouterr().out
    assert "Invited Telegram user 99 (paired)" in out


def test_pair_invite_missing_arg(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(run(settings, once="pair invite"))
    assert "Usage:" in capsys.readouterr().out


def test_pair_invite_invalid_id(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(run(settings, once="pair invite NaN"))
    assert "Invalid tg_user_id" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# `status` — server unreachable is the expected case in tests.
# --------------------------------------------------------------------------- #


def test_status_server_unreachable(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(run(settings, once="status"))
    out = capsys.readouterr().out
    assert "DB:" in out
    assert "Paired users: 0" in out
    assert "Pending codes: 0" in out
    assert "Server: unreachable" in out


def test_status_counts_active_only(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_paired_user(settings, tg_user_id=42, is_owner=True)
    _seed_pairing_code(settings, code="PEND0001", tg_user_id=99)
    asyncio.run(run(settings, once="status"))
    out = capsys.readouterr().out
    assert "Paired users: 1" in out
    assert "Pending codes: 1" in out


# --------------------------------------------------------------------------- #
# `exit` / `quit` and dispatch helpers.
# --------------------------------------------------------------------------- #


def test_exit_exits_quietly(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    asyncio.run(run(settings, once="exit"))
    out = capsys.readouterr().out
    # Exit must not print anything; once-mode swallows the sentinel.
    assert out == ""


def test_quit_exits_quietly(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    asyncio.run(run(settings, once="quit"))
    assert capsys.readouterr().out == ""


def test_dispatch_empty_input_is_noop(
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(dispatch("", settings))
    asyncio.run(dispatch("   ", settings))
    assert capsys.readouterr().out == ""


def test_commands_registry_keys() -> None:
    expected = {
        "pair list",
        "pair pending",
        "pair approve",
        "pair revoke",
        "pair invite",
        "status",
        "help",
        "exit",
        "quit",
    }
    assert expected.issubset(COMMANDS.keys())


# --------------------------------------------------------------------------- #
# Completer reflects DB state.
# --------------------------------------------------------------------------- #


def test_completer_includes_pending_codes_and_paired_ids(settings: Settings) -> None:
    _seed_paired_user(settings, tg_user_id=42, is_owner=True)
    _seed_pairing_code(settings, code="PEND0001", tg_user_id=99)
    completer = asyncio.run(build_completer(settings))
    words = list(completer.words)
    assert "pair" in words
    assert "approve" in words
    assert "PEND0001" in words
    assert "42" in words


def test_completer_excludes_revoked_users(settings: Settings) -> None:
    """A revoked user should not appear in revoke-target completions."""
    db_path = settings.data_dir / "ccr.db"
    # Insert a revoked user directly.
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO paired_users "
            "(id, tg_user_id, tg_username, label, is_owner, "
            " approved_by_tg_user_id, approved_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, 333, "bob", None, 0, None, now, now),
        )
        conn.commit()
    completer = asyncio.run(build_completer(settings))
    words = list(completer.words)
    assert "333" not in words


# --------------------------------------------------------------------------- #
# CLI integration — `python -m ccr console --once "<cmd>"`.
# --------------------------------------------------------------------------- #


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_cli_console_once_pair_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Drive the ``console --once`` flow end-to-end via the public CLI."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUBLIC_URL", "http://127.0.0.1:1/")
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(REPO_ROOT)

    main(["init-db"])
    capsys.readouterr()

    main(["console", "--once", "pair list"])
    assert capsys.readouterr().out.strip() == "(empty)"


def test_cli_console_once_help(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUBLIC_URL", "http://127.0.0.1:1/")
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(REPO_ROOT)

    main(["init-db"])
    capsys.readouterr()

    main(["console", "--once", "help"])
    out = capsys.readouterr().out
    assert "pair list" in out
    assert "status" in out
    assert "exit" in out


def test_cli_console_once_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUBLIC_URL", "http://127.0.0.1:1/")
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(REPO_ROOT)

    main(["init-db"])
    capsys.readouterr()

    main(["console", "--once", "totally_unknown"])
    assert "Unknown command. Type 'help'." in capsys.readouterr().out
