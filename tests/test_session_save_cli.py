"""End-to-end tests for ``python -m ccr session save <id>`` and the REPL twin.

Both the CLI subcommand and the REPL command call
:func:`ccr.claude.import_session.import_claude_session`. Tests use ``tmp_path``
for a fake ``~/.claude/projects/`` and seed the owner directly via SQL.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from ccr.claude.import_session import (
    ClaudeSessionFileNotFoundError,
    DuplicateClaudeSessionError,
    ImportSessionError,
    NoOwnerError,
    encoded_cwd_for,
    find_claude_session_file,
    import_claude_session,
)
from ccr.cli import main
from ccr.config import Settings
from ccr.db.engine import AsyncSessionMaker
from ccr.db.models import Base

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        telegram_bot_token="test-token",  # type: ignore[arg-type]
        public_url="http://127.0.0.1:1/",  # type: ignore[arg-type]
        jwt_secret="x" * 64,  # type: ignore[arg-type]
        data_dir=data_dir,
    )


async def _create_schema(settings: Settings) -> None:
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


def _seed_owner(settings: Settings, tg_user_id: int) -> None:
    db_path = settings.data_dir / "ccr.db"
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO paired_users "
            "(id, tg_user_id, tg_username, label, is_owner, "
            " approved_by_tg_user_id, approved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, tg_user_id, "owner", None, 1, None, now),
        )
        conn.commit()


def _make_claude_session_file(
    projects_root: Path,
    claude_session_id: str,
    *,
    body: str | None = None,
    cwd_dir: str = "-tmp-fake-cwd",
) -> Path:
    project_dir = projects_root / cwd_dir
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{claude_session_id}.jsonl"
    if body is None:
        body = (
            '{"type":"file-history-snapshot",'
            '"snapshot":{"timestamp":"2026-04-01T09:00:00Z"}}\n'
            '{"type":"user","timestamp":"2026-04-01T10:30:00Z",'
            '"message":{"role":"user","content":"hello"}}\n'
        )
    path.write_text(body, encoding="utf-8")
    return path


def _setup_env(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    projects_root: Path,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUBLIC_URL", "http://127.0.0.1:1/")
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(
        "ccr.claude.import_session._DEFAULT_PROJECTS_ROOT",
        projects_root,
    )


def _row_count(settings: Settings) -> int:
    with sqlite3.connect(settings.data_dir / "ccr.db") as conn:
        cur = conn.execute("SELECT COUNT(*) FROM sessions")
        return int(cur.fetchone()[0])


def _row_columns(settings: Settings, claude_session_id: str) -> dict[str, object]:
    with sqlite3.connect(settings.data_dir / "ccr.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, status, started_by_tg_user_id, started_at, "
            "claude_session_id, first_prompt "
            "FROM sessions WHERE claude_session_id = ?",
            (claude_session_id,),
        ).fetchone()
        if row is None:
            return {}
        return dict(row)


# --------------------------------------------------------------------------- #
# Direct unit tests of the helpers.
# --------------------------------------------------------------------------- #


def test_encoded_cwd_for_round_trips_simple_path() -> None:
    assert encoded_cwd_for(Path("/Users/edward/foo")) == "-Users-edward-foo"


# --------------------------------------------------------------------------- #
# CLI flow.
# --------------------------------------------------------------------------- #


def test_session_save_cli_imports_local_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _make_settings(tmp_path)
    asyncio.run(_create_schema(settings))
    _seed_owner(settings, tg_user_id=42)

    projects_root = tmp_path / "claude_projects"
    cs_id = "11111111-1111-4111-8111-111111111111"
    _make_claude_session_file(projects_root, cs_id)

    _setup_env(monkeypatch, settings, projects_root)
    main(["session", "save", cs_id])

    cols = _row_columns(settings, cs_id)
    assert cols, "row was not inserted"
    assert cols["claude_session_id"] == cs_id
    assert cols["status"] == "stopped"
    assert cols["started_by_tg_user_id"] == 42
    assert cols["first_prompt"] is None
    started_at = datetime.fromisoformat(str(cols["started_at"]))
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    assert started_at == datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC)


def test_session_save_console_once_imports_local_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _make_settings(tmp_path)
    asyncio.run(_create_schema(settings))
    _seed_owner(settings, tg_user_id=42)

    projects_root = tmp_path / "claude_projects"
    cs_id = "22222222-2222-4222-8222-222222222222"
    _make_claude_session_file(projects_root, cs_id)

    _setup_env(monkeypatch, settings, projects_root)
    main(["console", "--once", f"session save {cs_id}"])

    cols = _row_columns(settings, cs_id)
    assert cols, "row was not inserted"
    assert cols["claude_session_id"] == cs_id
    assert cols["status"] == "stopped"
    assert cols["started_by_tg_user_id"] == 42


def test_session_save_double_import_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _make_settings(tmp_path)
    asyncio.run(_create_schema(settings))
    _seed_owner(settings, tg_user_id=42)

    projects_root = tmp_path / "claude_projects"
    cs_id = "33333333-3333-4333-8333-333333333333"
    _make_claude_session_file(projects_root, cs_id)

    _setup_env(monkeypatch, settings, projects_root)
    main(["session", "save", cs_id])
    capsys.readouterr()

    with pytest.raises(SystemExit) as ei:
        main(["session", "save", cs_id])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "Already imported" in err
    assert _row_count(settings) == 1


def test_session_save_unknown_id_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _make_settings(tmp_path)
    asyncio.run(_create_schema(settings))
    _seed_owner(settings, tg_user_id=42)

    projects_root = tmp_path / "claude_projects"
    projects_root.mkdir(parents=True)

    _setup_env(monkeypatch, settings, projects_root)
    missing_id = "44444444-4444-4444-8444-444444444444"
    with pytest.raises(SystemExit) as ei:
        main(["session", "save", missing_id])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert f"No local Claude session found with id {missing_id}" in err
    assert _row_count(settings) == 0


def test_session_save_no_owner_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _make_settings(tmp_path)
    asyncio.run(_create_schema(settings))
    # No owner seeded.

    projects_root = tmp_path / "claude_projects"
    cs_id = "55555555-5555-4555-8555-555555555555"
    _make_claude_session_file(projects_root, cs_id)

    _setup_env(monkeypatch, settings, projects_root)
    with pytest.raises(SystemExit) as ei:
        main(["session", "save", cs_id])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "No owner registered" in err
    assert _row_count(settings) == 0


def test_session_save_uses_first_top_level_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leading ``file-history-snapshot`` (no top-level timestamp) is skipped.

    The first ``type:"user"`` line carries a top-level ``timestamp`` and that
    is what populates ``started_at``.
    """
    settings = _make_settings(tmp_path)
    asyncio.run(_create_schema(settings))
    _seed_owner(settings, tg_user_id=42)

    projects_root = tmp_path / "claude_projects"
    cs_id = "66666666-6666-4666-8666-666666666666"
    body = (
        '{"type":"file-history-snapshot",'
        '"snapshot":{"timestamp":"2020-01-01T00:00:00Z"}}\n'
        '{"type":"user","timestamp":"2026-04-15T08:42:00Z",'
        '"message":{"role":"user","content":"first prompt"}}\n'
        '{"type":"user","timestamp":"2026-04-15T09:00:00Z",'
        '"message":{"role":"user","content":"second prompt"}}\n'
    )
    _make_claude_session_file(projects_root, cs_id, body=body)

    _setup_env(monkeypatch, settings, projects_root)
    main(["session", "save", cs_id])

    cols = _row_columns(settings, cs_id)
    started_at = datetime.fromisoformat(str(cols["started_at"]))
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    assert started_at == datetime(2026, 4, 15, 8, 42, 0, tzinfo=UTC)


def test_session_save_does_not_copy_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _make_settings(tmp_path)
    asyncio.run(_create_schema(settings))
    _seed_owner(settings, tg_user_id=42)

    projects_root = tmp_path / "claude_projects"
    cs_id = "77777777-7777-4777-8777-777777777777"
    _make_claude_session_file(projects_root, cs_id)

    _setup_env(monkeypatch, settings, projects_root)
    main(["session", "save", cs_id])

    cols = _row_columns(settings, cs_id)
    row_id_hex = str(cols["id"]).replace("-", "")
    logs_dir = settings.data_dir / "logs"
    # No data/logs/<our_uuid>.jsonl is created on import.
    if logs_dir.exists():
        assert not any(logs_dir.iterdir())
    # Defensive: the specific path the manager would create on continuation
    # is still absent.
    candidate_a = logs_dir / f"{row_id_hex}.jsonl"
    candidate_b = logs_dir / f"{cols['id']}.jsonl"
    assert not candidate_a.exists()
    assert not candidate_b.exists()


# --------------------------------------------------------------------------- #
# Direct unit tests of error paths.
# --------------------------------------------------------------------------- #


def test_find_claude_session_file_raises_on_miss(tmp_path: Path) -> None:
    projects_root = tmp_path / "claude_projects"
    projects_root.mkdir()
    missing_id = "88888888-8888-4888-8888-888888888888"
    with pytest.raises(ClaudeSessionFileNotFoundError):
        find_claude_session_file(missing_id, projects_root=projects_root)


async def _run_import(
    settings: Settings,
    claude_session_id: str,
    *,
    projects_root: Path | None = None,
) -> object:
    from ccr.db.engine import create_engine_from_settings

    engine = create_engine_from_settings(settings)
    factory = AsyncSessionMaker(engine)
    try:
        async with factory() as session:
            return await import_claude_session(
                session,
                claude_session_id,
                projects_root=projects_root,
            )
    finally:
        await engine.dispose()


def test_import_claude_session_raises_no_owner_when_unowned(
    tmp_path: Path,
) -> None:
    settings = _make_settings(tmp_path)
    asyncio.run(_create_schema(settings))

    projects_root = tmp_path / "claude_projects"
    cs_id = "99999999-9999-4999-8999-999999999999"
    _make_claude_session_file(projects_root, cs_id)

    with pytest.raises(NoOwnerError):
        asyncio.run(_run_import(settings, cs_id, projects_root=projects_root))


def test_import_claude_session_raises_duplicate_on_second_import(
    tmp_path: Path,
) -> None:
    """CCR-041: programmatic SELECT-before-INSERT raises ``DuplicateClaudeSessionError``.

    The DB-side UNIQUE flag on ``ix_sessions_claude_session_id_not_null`` was
    dropped (CCR-041); the import-time double-write guard now lives as a
    SELECT-before-INSERT inside :func:`import_claude_session`. The exception
    must be raised directly, NOT chained from a SQLAlchemy
    :class:`~sqlalchemy.exc.IntegrityError`.
    """
    from sqlalchemy.exc import IntegrityError

    settings = _make_settings(tmp_path)
    asyncio.run(_create_schema(settings))
    _seed_owner(settings, tg_user_id=42)

    projects_root = tmp_path / "claude_projects"
    cs_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _make_claude_session_file(projects_root, cs_id)

    asyncio.run(_run_import(settings, cs_id, projects_root=projects_root))
    with pytest.raises(DuplicateClaudeSessionError) as ei:
        asyncio.run(_run_import(settings, cs_id, projects_root=projects_root))
    # Programmatic guard fires before the INSERT, so there is no IntegrityError
    # in the cause chain.
    chain: list[BaseException] = []
    cur: BaseException | None = ei.value.__cause__
    while cur is not None:
        chain.append(cur)
        cur = cur.__cause__
    assert not any(isinstance(e, IntegrityError) for e in chain)


# --------------------------------------------------------------------------- #
# CCR-036 F3: path-traversal / format guard.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../etc/passwd",
        "../foo",
        "/absolute/path",
        "not-a-uuid-at-all",
    ],
)
def test_session_save_rejects_path_separators(
    tmp_path: Path,
    bad_id: str,
) -> None:
    """Malformed Claude session ids must not reach the filesystem.

    The id is interpolated into ``<projects_root>/<dir>/<id>.jsonl`` so any
    input containing ``/``, ``\\``, ``..``, or anything else outside the
    UUID4 grammar must be rejected with a clear :class:`ImportSessionError`
    *before* any filesystem access happens.
    """
    settings = _make_settings(tmp_path)
    asyncio.run(_create_schema(settings))
    _seed_owner(settings, tg_user_id=42)

    projects_root = tmp_path / "claude_projects"
    projects_root.mkdir(parents=True)

    with pytest.raises(ImportSessionError, match="Invalid Claude session id format"):
        asyncio.run(_run_import(settings, bad_id, projects_root=projects_root))


def test_session_save_accepts_valid_uuid4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The format guard must NOT reject canonical UUID4 ids (happy path)."""
    settings = _make_settings(tmp_path)
    asyncio.run(_create_schema(settings))
    _seed_owner(settings, tg_user_id=42)

    projects_root = tmp_path / "claude_projects"
    cs_id = "a1b2c3d4-e5f6-4789-89ab-cdef01234567"
    _make_claude_session_file(projects_root, cs_id)

    _setup_env(monkeypatch, settings, projects_root)
    main(["session", "save", cs_id])

    cols = _row_columns(settings, cs_id)
    assert cols, "row was not inserted"
    assert cols["claude_session_id"] == cs_id
