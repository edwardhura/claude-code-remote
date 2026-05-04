"""Tests for :class:`ccr.claude.SessionManager` using the fake `claude` binary.

We point ``settings.claude_bin`` at the executable shim
``tests/fakes/fake_claude`` so subprocess invocations exercise the
:mod:`tests.fakes.fake_claude` module via a plain `python -m` invocation.
"""

from __future__ import annotations

import asyncio
import json
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

from ccr.claude import (
    ClaudeEvent,
    NoActiveSessionError,
    SessionManager,
    SessionStatus,
)
from ccr.claude.events import UnknownEvent
from ccr.claude.manager import (
    NoPriorSessionError,
    SessionAlreadyRunningError,
    SessionNotFoundError,
)
from ccr.config import Settings
from ccr.db.engine import AsyncSessionMaker
from ccr.db.models import Base, Session
from ccr.events import EventBus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine


REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = REPO_ROOT / "tests" / "fakes" / "fake_claude"


# --------------------------------------------------------------------------- #
# Fixtures.
# --------------------------------------------------------------------------- #


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
        claude_bin=str(FAKE_CLAUDE),
        subprocess_grace_kill_seconds=2,
    )


def _write_script(tmp_path: Path, events: list[dict[str, object]]) -> Path:
    path = tmp_path / "script.jsonl"
    path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )
    return path


def _set_fake_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    script: Path,
    abort_after: int | None = None,
    exit_code: int = 0,
    delay_ms: int = 0,
    stderr: str | None = None,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(script))
    monkeypatch.setenv("FAKE_CLAUDE_DELAY_MS", str(delay_ms))
    monkeypatch.setenv("FAKE_CLAUDE_EXIT_CODE", str(exit_code))
    if abort_after is not None:
        monkeypatch.setenv("FAKE_CLAUDE_ABORT_AFTER", str(abort_after))
    if stderr is not None:
        monkeypatch.setenv("FAKE_CLAUDE_STDERR", stderr)
    # Make sure tests.fakes is importable when fake_claude shim runs.
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))


async def _drain_status(
    bus: EventBus, target: SessionStatus, timeout: float = 5.0
) -> dict[str, object]:
    """Wait for a session.status payload matching ``target``."""

    async def _wait() -> dict[str, object]:
        async for payload in bus.subscribe("session.status"):
            assert isinstance(payload, dict)
            if payload.get("status") == target:
                return payload
        msg = "subscription closed without target status"
        raise AssertionError(msg)

    return await asyncio.wait_for(_wait(), timeout=timeout)


# --------------------------------------------------------------------------- #
# Tests.
# --------------------------------------------------------------------------- #


async def test_lifecycle_idle_to_running_to_completed(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {"type": "system", "subtype": "init", "session_id": "fake"},
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "world"}],
            },
        },
        {"type": "result", "subtype": "success", "duration_ms": 10},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    received: list[ClaudeEvent] = []

    async def consumer() -> None:
        async for payload in bus.subscribe("session.event"):
            assert isinstance(payload, dict)
            received.append(payload["event"])  # type: ignore[arg-type]
            if len(received) == 5:
                break

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)

    assert (await manager.status()) == SessionStatus.IDLE

    session_id = await manager.new_session(prompt=None, started_by_tg_user_id=42)
    assert (await manager.status()) == SessionStatus.RUNNING

    await asyncio.wait_for(task, timeout=5.0)
    assert len(received) == 5

    await _drain_status(bus, SessionStatus.COMPLETED)
    assert (await manager.status()) == SessionStatus.COMPLETED

    log_path = tmp_path / "logs" / f"{session_id}.jsonl"
    assert log_path.exists()
    lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 5

    async with session_factory() as db:
        row = await db.scalar(select(Session).where(Session.id == session_id))
        assert row is not None
        assert row.status == "completed"
        assert row.started_by_tg_user_id == 42


async def test_subprocess_kill_marks_session_crashed_and_emits_error_event(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {"type": "system", "subtype": "init", "session_id": "fake"},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "partway"}],
            },
        },
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(
        monkeypatch,
        script=script,
        abort_after=1,
        exit_code=1,
        stderr="boom\n",
    )

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    received: list[ClaudeEvent] = []

    async def consumer() -> None:
        async for payload in bus.subscribe("session.event"):
            assert isinstance(payload, dict)
            received.append(payload["event"])  # type: ignore[arg-type]
            if any(isinstance(e, UnknownEvent) and e.type == "error" for e in received):
                break

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)

    session_id = await manager.new_session(prompt=None, started_by_tg_user_id=None)

    await asyncio.wait_for(task, timeout=5.0)
    assert any(isinstance(e, UnknownEvent) and e.type == "error" for e in received), received

    await _drain_status(bus, SessionStatus.CRASHED)

    async with session_factory() as db:
        row = await db.scalar(select(Session).where(Session.id == session_id))
        assert row is not None
        assert row.status == "crashed"
        assert row.exit_reason is not None


async def test_stop_is_idempotent(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)
    # No active session — both calls are no-ops.
    await manager.stop()
    await manager.stop()
    assert (await manager.status()) == SessionStatus.IDLE


async def test_new_session_while_running_stops_old_first(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    # Slow it down so the first session is still running when we replace it.
    _set_fake_env(monkeypatch, script=script, delay_ms=200)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    first = await manager.new_session(prompt=None, started_by_tg_user_id=None)
    # Allow the subprocess to register at least the init event before
    # we tear it down.
    await asyncio.sleep(0.1)

    second = await manager.new_session(prompt=None, started_by_tg_user_id=None)
    assert first != second

    # Wait for the second session to finish.
    await _drain_status(bus, SessionStatus.COMPLETED, timeout=5.0)

    async with session_factory() as db:
        row_a = await db.scalar(select(Session).where(Session.id == first))
        row_b = await db.scalar(select(Session).where(Session.id == second))
    assert row_a is not None
    assert row_b is not None
    assert row_a.status == "stopped"
    assert row_b.status == "completed"


async def test_send_without_active_session_raises(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)
    with pytest.raises(NoActiveSessionError):
        await manager.send("hello")


async def test_first_prompt_recorded_truncated_to_500(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    long_prompt = "a" * 1000
    session_id = await manager.new_session(prompt=long_prompt, started_by_tg_user_id=None)

    await _drain_status(bus, SessionStatus.COMPLETED, timeout=5.0)

    async with session_factory() as db:
        row = await db.scalar(select(Session).where(Session.id == session_id))
    assert row is not None
    assert row.first_prompt is not None
    assert len(row.first_prompt) == 500


async def test_last_event_at_is_updated_with_debounce(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "x"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "y"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "z"}],
            },
        },
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    session_id = await manager.new_session(prompt=None, started_by_tg_user_id=None)
    await _drain_status(bus, SessionStatus.COMPLETED, timeout=5.0)

    async with session_factory() as db:
        row = await db.scalar(select(Session).where(Session.id == session_id))
    assert row is not None
    # last_event_at should have been written at least once. Debounce limits
    # the number of writes, but we can only assert a value is present.
    assert row.last_event_at is not None


# --------------------------------------------------------------------------- #
# CCR-020: /continue — resume the most recent finished session.
# --------------------------------------------------------------------------- #


async def _seed_finished_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    session_id: object,
    started_at: object,
    status: str = "completed",
) -> None:
    """Insert a finished :class:`Session` row directly for continue-session tests."""
    import uuid as _uuid_mod
    from datetime import datetime as _dt_mod

    assert isinstance(session_id, _uuid_mod.UUID)
    assert isinstance(started_at, _dt_mod)
    async with factory() as db:
        db.add(
            Session(
                id=session_id,
                started_at=started_at,
                status=status,
                started_by_tg_user_id=None,
                first_prompt=None,
            ),
        )
        await db.commit()


async def test_continue_session_passes_resume_flag_to_subprocess(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outcome 1: ``continue_session`` adds ``--continue`` to the subprocess argv."""
    import uuid as _uuid_mod
    from datetime import UTC as _UTC
    from datetime import datetime as _dt_mod

    await _seed_finished_session(
        session_factory,
        session_id=_uuid_mod.UUID("11111111-1111-1111-1111-111111111111"),
        started_at=_dt_mod(2026, 4, 30, 10, 0, 0, tzinfo=_UTC),
        status="completed",
    )

    events = [
        {"type": "system", "subtype": "init", "session_id": "fake"},
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script)
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    new_id = await manager.continue_session(started_by_tg_user_id=42)
    await _drain_status(bus, SessionStatus.COMPLETED, timeout=5.0)

    assert isinstance(new_id, _uuid_mod.UUID)
    assert argv_file.exists()
    argv_lines = argv_file.read_text(encoding="utf-8").splitlines()
    assert "--continue" in argv_lines
    # Outcome 1: no --resume flag, no claude session id is plumbed through.
    assert "--resume" not in argv_lines


async def test_continue_session_with_no_prior_session_raises(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An empty DB raises :class:`NoPriorSessionError` with the canned message."""
    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    with pytest.raises(NoPriorSessionError) as ei:
        await manager.continue_session(started_by_tg_user_id=42)
    assert str(ei.value) == "No prior session to continue."


async def test_continue_session_while_running_raises(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling continue_session while a session is already running raises with the canned message."""
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    # Slow it down so the first session is still running when we try to continue.
    _set_fake_env(monkeypatch, script=script, delay_ms=200)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)
    await manager.new_session(prompt=None, started_by_tg_user_id=None)

    with pytest.raises(SessionAlreadyRunningError) as ei:
        await manager.continue_session(started_by_tg_user_id=42)
    assert str(ei.value) == "Session already running. /stop first or /clear to start fresh."

    await manager.stop()


async def test_continue_session_picks_most_recent_finished(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crashed rows are excluded from the resumable lookup; the newest stopped row wins.

    Seed: oldest=completed, middle=stopped, newest=crashed. Expectation:
    the lookup picks the middle (stopped) row, not the newest (crashed).
    """
    import uuid as _uuid_mod
    from datetime import UTC as _UTC
    from datetime import datetime as _dt_mod
    from datetime import timedelta as _td

    base = _dt_mod(2026, 4, 30, 10, 0, 0, tzinfo=_UTC)
    sid_oldest = _uuid_mod.UUID("aaaaaaaa-1111-1111-1111-111111111111")
    sid_middle = _uuid_mod.UUID("bbbbbbbb-2222-2222-2222-222222222222")
    sid_newest = _uuid_mod.UUID("cccccccc-3333-3333-3333-333333333333")
    await _seed_finished_session(
        session_factory,
        session_id=sid_oldest,
        started_at=base,
        status="completed",
    )
    await _seed_finished_session(
        session_factory,
        session_id=sid_middle,
        started_at=base + _td(seconds=10),
        status="stopped",
    )
    await _seed_finished_session(
        session_factory,
        session_id=sid_newest,
        started_at=base + _td(seconds=20),
        status="crashed",
    )

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    prior_id, _ = await manager._db_lookup_most_recent_finished()  # noqa: SLF001 — direct internal probe
    assert prior_id == sid_middle

    # End-to-end sanity: continue_session succeeds (would have raised
    # NoPriorSessionError if crashed had been mistakenly picked above and
    # excluded by some other guard).
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script)
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))

    new_id = await manager.continue_session(started_by_tg_user_id=42)
    assert new_id not in {sid_oldest, sid_middle, sid_newest}
    await _drain_status(bus, SessionStatus.COMPLETED, timeout=5.0)


async def test_continue_session_inserts_new_row_with_running_status(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``continue_session`` inserts a NEW row; the prior row is unchanged."""
    import uuid as _uuid_mod
    from datetime import UTC as _UTC
    from datetime import datetime as _dt_mod

    prior_id = _uuid_mod.UUID("dddddddd-4444-4444-4444-444444444444")
    await _seed_finished_session(
        session_factory,
        session_id=prior_id,
        started_at=_dt_mod(2026, 4, 30, 11, 0, 0, tzinfo=_UTC),
        status="completed",
    )

    # Slow the producer so we can observe RUNNING before the script
    # finishes and the row flips to COMPLETED.
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script, delay_ms=200)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    new_id = await manager.continue_session(started_by_tg_user_id=99)
    assert new_id != prior_id

    # Inspect the row immediately after creation: it should be RUNNING.
    async with session_factory() as db:
        row = await db.scalar(select(Session).where(Session.id == new_id))
    assert row is not None
    assert row.status == SessionStatus.RUNNING.value
    assert row.started_by_tg_user_id == 99
    assert row.first_prompt is None

    # Prior row is untouched.
    async with session_factory() as db:
        prior_row = await db.scalar(select(Session).where(Session.id == prior_id))
    assert prior_row is not None
    assert prior_row.status == "completed"

    await manager.stop()


# --------------------------------------------------------------------------- #
# CCR-020 extension: /continue <8-hex-prefix>.
# --------------------------------------------------------------------------- #


async def test_continue_session_with_prefix_resumes_matched_row(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid prefix selects the matching resumable row even when newer rows exist."""
    import uuid as _uuid_mod
    from datetime import UTC as _UTC
    from datetime import datetime as _dt_mod
    from datetime import timedelta as _td

    base = _dt_mod(2026, 4, 30, 10, 0, 0, tzinfo=_UTC)
    older_id = _uuid_mod.UUID("76581b99-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    newer_id = _uuid_mod.UUID("ffffffff-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    await _seed_finished_session(
        session_factory,
        session_id=older_id,
        started_at=base,
        status="completed",
    )
    await _seed_finished_session(
        session_factory,
        session_id=newer_id,
        started_at=base + _td(seconds=30),
        status="completed",
    )

    events = [
        {"type": "system", "subtype": "init", "session_id": "fake"},
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script)
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    new_id = await manager.continue_session(
        started_by_tg_user_id=42,
        session_id_prefix="76581b99",
    )
    assert new_id not in {older_id, newer_id}
    await _drain_status(bus, SessionStatus.COMPLETED, timeout=5.0)

    # The fresh row references the older row's lineage in the sense that
    # --continue was passed; verify the subprocess argv contains it.
    assert argv_file.exists()
    argv_lines = argv_file.read_text(encoding="utf-8").splitlines()
    assert "--continue" in argv_lines

    # The older row is the one we picked: it must still be present and
    # untouched (status == completed). The newer row is also untouched —
    # neither row's ``status`` should have changed because we only read.
    async with session_factory() as db:
        older_row = await db.scalar(select(Session).where(Session.id == older_id))
        newer_row = await db.scalar(select(Session).where(Session.id == newer_id))
    assert older_row is not None
    assert older_row.status == "completed"
    assert newer_row is not None
    assert newer_row.status == "completed"

    # And the helper itself returns the older id when called directly with
    # the prefix — the canonical assertion that the older row was selected.
    prior_id, _ = await manager._db_lookup_session_by_prefix("76581b99")  # noqa: SLF001
    assert prior_id == older_id


async def test_continue_session_with_unknown_prefix_raises_not_found(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An 8-hex prefix that matches no resumable row raises ``SessionNotFoundError``."""
    import uuid as _uuid_mod
    from datetime import UTC as _UTC
    from datetime import datetime as _dt_mod

    await _seed_finished_session(
        session_factory,
        session_id=_uuid_mod.UUID("76581b99-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        started_at=_dt_mod(2026, 4, 30, 10, 0, 0, tzinfo=_UTC),
        status="completed",
    )

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    with pytest.raises(SessionNotFoundError) as ei:
        await manager.continue_session(
            started_by_tg_user_id=42,
            session_id_prefix="00000000",
        )
    assert str(ei.value) == "No session found with id 00000000."


async def test_continue_session_lookup_by_prefix_excludes_crashed(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Crashed rows are excluded from the prefix lookup, mirroring the no-arg path."""
    import uuid as _uuid_mod
    from datetime import UTC as _UTC
    from datetime import datetime as _dt_mod

    crashed_id = _uuid_mod.UUID("deadbeef-cafe-cafe-cafe-cafecafecafe")
    await _seed_finished_session(
        session_factory,
        session_id=crashed_id,
        started_at=_dt_mod(2026, 4, 30, 10, 0, 0, tzinfo=_UTC),
        status="crashed",
    )

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    with pytest.raises(SessionNotFoundError) as ei:
        await manager.continue_session(
            started_by_tg_user_id=42,
            session_id_prefix="deadbeef",
        )
    assert str(ei.value) == "No session found with id deadbeef."


async def test_db_lookup_session_by_prefix_returns_none_for_no_match(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Direct helper call on an empty DB returns ``(None, None)``."""
    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    prior_id, claude_id = await manager._db_lookup_session_by_prefix("12345678")  # noqa: SLF001
    assert prior_id is None
    assert claude_id is None


# --------------------------------------------------------------------------- #
# CCR-025: MCP permission gate plumbing.
# --------------------------------------------------------------------------- #


async def test_resolve_permission_happy_path(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Manager forwards :meth:`resolve_permission` to the MCP server's Future map."""
    import uuid as _uuid_mod

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)
    sid = _uuid_mod.UUID("88888888-8888-8888-8888-888888888888")
    manager._mcp.set_current_session(sid)  # noqa: SLF001 — direct internal probe

    async def _drive_tool_call() -> dict[str, object]:
        return await manager._mcp._on_tool_call("Bash", {"cmd": "ls"})  # noqa: SLF001

    tool_task = asyncio.create_task(_drive_tool_call())
    # Give the tool call a tick to register the Future before we resolve.
    await asyncio.sleep(0.01)
    pending = list(manager._mcp._futures.keys())  # noqa: SLF001
    assert len(pending) == 1
    request_id = pending[0]
    assert manager.is_permission_pending(request_id) is True

    ok = await manager.resolve_permission(
        request_id,
        {"behavior": "allow", "updatedInput": {"x": 1}},
    )
    assert ok is True

    payload = await asyncio.wait_for(tool_task, timeout=1.0)
    assert payload == {"behavior": "allow", "updatedInput": {"x": 1}}


async def test_resolve_permission_unknown_request_id_returns_false(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)
    ok = await manager.resolve_permission("nope", {"behavior": "deny", "message": "x"})
    assert ok is False
    assert manager.is_permission_pending("nope") is False


async def test_teardown_cancels_pending_permissions_with_deny(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``manager.stop()`` must cancel any outstanding permission Futures with deny."""
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script, delay_ms=200)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    session_id = await manager.new_session(prompt=None, started_by_tg_user_id=None)

    # Drive an in-flight permission tool call against the manager's MCP server.
    async def _drive() -> dict[str, object]:
        return await manager._mcp._on_tool_call("Edit", {})  # noqa: SLF001

    tool_task = asyncio.create_task(_drive())
    await asyncio.sleep(0.01)
    request_ids = list(manager._mcp._futures.keys())  # noqa: SLF001
    assert request_ids, "expected one in-flight permission Future"

    await manager.stop()
    payload = await asyncio.wait_for(tool_task, timeout=1.0)
    assert payload["behavior"] == "deny"
    assert manager.is_permission_pending(request_ids[0]) is False
    import uuid as _uuid_mod

    assert isinstance(session_id, _uuid_mod.UUID)


async def test_new_session_starts_mcp_server_idempotently(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ``new_session`` calls share one MCP server start."""
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    call_count = 0
    real_start = manager._mcp.start  # noqa: SLF001

    async def _spy_start() -> None:
        nonlocal call_count
        call_count += 1
        await real_start()

    manager._mcp.start = _spy_start  # type: ignore[method-assign] # noqa: SLF001

    await manager.new_session(prompt=None, started_by_tg_user_id=None)
    await _drain_status(bus, SessionStatus.COMPLETED, timeout=5.0)

    await manager.new_session(prompt=None, started_by_tg_user_id=None)
    await _drain_status(bus, SessionStatus.COMPLETED, timeout=5.0)

    # Real start always returns early on the second call (idempotency
    # guard inside McpPermissionServer); the spy still counts every
    # invocation, but only the first should trigger work because
    # _mcp_started flips to True after the first new_session.
    assert call_count == 1


async def test_new_session_argv_includes_mcp_flags(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subprocess argv must carry ``--permission-prompt-tool`` and ``--mcp-config``."""
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script)
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    await manager.new_session(prompt=None, started_by_tg_user_id=None)
    await _drain_status(bus, SessionStatus.COMPLETED, timeout=5.0)

    argv_lines = argv_file.read_text(encoding="utf-8").splitlines()
    assert "--permission-prompt-tool" in argv_lines
    assert "mcp__ccr__ccr_permission_prompt" in argv_lines
    assert "--mcp-config" in argv_lines


# --------------------------------------------------------------------------- #
# CCR-022: running_subagents() — Task/Agent tool tracking.
# --------------------------------------------------------------------------- #


async def test_running_subagents_tracks_agent_tool_use_and_clears_on_result(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A ``Task`` tool_use registers the subagent; the matching tool_result clears it.

    Drives synthetic events through ``SessionManager._track_subagents`` —
    the same code path :meth:`SessionManager._consume_events` runs after
    each event publishes — which lets the test exercise the snapshot
    accessor deterministically without subprocess timing.
    """
    from ccr.claude.events import parse_event

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    # No session running → empty snapshot.
    assert manager.running_subagents() == []

    tool_use_id = "toolu_test_001"
    task_event = parse_event(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": "Task",
                        "input": {
                            "subagent_type": "python-developer",
                            "description": "Test dispatch",
                            "prompt": "go",
                        },
                    },
                ],
            },
        }
    )
    manager._track_subagents(task_event)  # noqa: SLF001 — direct internal probe
    assert manager.running_subagents() == ["python-developer"]

    result_event = parse_event(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": "done",
                        "is_error": False,
                    },
                ],
            },
        }
    )
    manager._track_subagents(result_event)  # noqa: SLF001 — direct internal probe
    assert manager.running_subagents() == []


async def test_running_subagents_ignores_non_subagent_tools(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``Bash`` tool_use is not tracked; ``Agent`` (legacy) is.

    Confirms the :data:`_SUBAGENT_DISPATCH_TOOL_NAMES` filter and the
    legacy ``"Agent"`` name. Same direct-probe rationale as above.
    """
    from ccr.claude.events import parse_event

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    bash_event = parse_event(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_bash_001",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
        }
    )
    manager._track_subagents(bash_event)  # noqa: SLF001
    assert manager.running_subagents() == []

    agent_event = parse_event(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_agent_001",
                        "name": "Agent",
                        "input": {
                            "subagent_type": "architect",
                            "description": "design",
                            "prompt": "design something",
                        },
                    },
                ],
            },
        }
    )
    manager._track_subagents(agent_event)  # noqa: SLF001
    assert manager.running_subagents() == ["architect"]


# --------------------------------------------------------------------------- #
# CCR-023: current_session_usage() — JSONL aggregator wired to live session.
# --------------------------------------------------------------------------- #


async def test_current_session_usage_returns_none_when_idle(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``current_session_usage()`` returns ``None`` while no subprocess is held."""
    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)
    assert manager.current_session_usage() is None


async def test_current_session_usage_aggregates_running_session_jsonl(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a session is running, the accessor walks ``data/logs/<id>.jsonl``."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "fake"},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}},
                ],
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "duration_ms": 1500,
            "total_cost_usd": 0.01,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 500,
            },
        },
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    received: list[ClaudeEvent] = []

    async def consumer() -> None:
        async for payload in bus.subscribe("session.event"):
            assert isinstance(payload, dict)
            received.append(payload["event"])  # type: ignore[arg-type]
            if len(received) == len(events):
                break

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)

    await manager.new_session(prompt=None, started_by_tg_user_id=1)
    await asyncio.wait_for(task, timeout=5.0)

    # Subprocess is still being torn down at this point — the in-memory
    # status is RUNNING but new_session has returned. Aggregator should
    # see all three events on disk because _consume_events writes to the
    # log before publishing to the bus.
    usage = manager.current_session_usage()
    assert usage is not None
    assert usage.input_tokens == 10
    assert usage.output_tokens == 20
    assert usage.cache_creation_input_tokens == 100
    assert usage.cache_read_input_tokens == 500
    assert usage.tool_call_count == 1
    assert usage.num_turns == 1
    assert usage.elapsed_ms == 1500
    assert abs(usage.total_cost_usd - 0.01) < 1e-9

    # Wait for the session to complete so the test does not leak tasks.
    await _drain_status(bus, SessionStatus.COMPLETED)


async def test_current_session_usage_returns_none_after_stop(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once :meth:`SessionManager.stop` tears down, the accessor returns ``None``."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "fake"},
        {"type": "result", "subtype": "success", "duration_ms": 10},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    await manager.new_session(prompt=None, started_by_tg_user_id=1)
    await _drain_status(bus, SessionStatus.COMPLETED)
    await manager.stop()

    assert manager.current_session_usage() is None


# --------------------------------------------------------------------------- #
# CCR-032: current_rate_limit_status() — last-one-wins per-line snapshot.
# --------------------------------------------------------------------------- #


async def test_current_rate_limit_status_idle_returns_none(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``current_rate_limit_status()`` returns ``None`` while no subprocess is held."""
    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)
    assert manager.current_rate_limit_status() is None


async def test_current_rate_limit_status_populates_after_event(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``rate_limit_event`` in the JSONL stream is captured into the snapshot."""
    from ccr.claude.events import RateLimitEvent

    events = [
        {"type": "system", "subtype": "init", "session_id": "fake"},
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "resetsAt": 1777861800,
                "rateLimitType": "five_hour",
                "overageStatus": "rejected",
                "overageDisabledReason": "group_zero_credit_limit",
                "isUsingOverage": False,
            },
            "uuid": "758cb741-0e54-4af1-9c32-0b76648f795f",
            "session_id": "fake",
        },
        {"type": "result", "subtype": "success", "duration_ms": 10},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    received: list[ClaudeEvent] = []

    async def consumer() -> None:
        async for payload in bus.subscribe("session.event"):
            assert isinstance(payload, dict)
            received.append(payload["event"])  # type: ignore[arg-type]
            if len(received) == len(events):
                break

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)

    await manager.new_session(prompt=None, started_by_tg_user_id=None)
    await asyncio.wait_for(task, timeout=5.0)

    rl = manager.current_rate_limit_status()
    assert isinstance(rl, RateLimitEvent)
    assert rl.rate_limit_info is not None
    assert rl.rate_limit_info.rate_limit_type == "five_hour"
    assert rl.rate_limit_info.status == "allowed"
    assert rl.rate_limit_info.is_using_overage is False

    await _drain_status(bus, SessionStatus.COMPLETED)


async def test_current_rate_limit_status_last_event_wins(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When two ``rate_limit_event`` lines arrive, the second snapshot wins."""
    from ccr.claude.events import RateLimitEvent

    events = [
        {"type": "system", "subtype": "init", "session_id": "fake"},
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "rateLimitType": "five_hour",
            },
        },
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "warning",
                "rateLimitType": "weekly",
            },
        },
        {"type": "result", "subtype": "success", "duration_ms": 10},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    received: list[ClaudeEvent] = []

    async def consumer() -> None:
        async for payload in bus.subscribe("session.event"):
            assert isinstance(payload, dict)
            received.append(payload["event"])  # type: ignore[arg-type]
            if len(received) == len(events):
                break

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)

    await manager.new_session(prompt=None, started_by_tg_user_id=None)
    await asyncio.wait_for(task, timeout=5.0)

    rl = manager.current_rate_limit_status()
    assert isinstance(rl, RateLimitEvent)
    assert rl.rate_limit_info is not None
    # The second event must overwrite the first.
    assert rl.rate_limit_info.rate_limit_type == "weekly"
    assert rl.rate_limit_info.status == "warning"

    await _drain_status(bus, SessionStatus.COMPLETED)


async def test_current_rate_limit_status_resets_on_stop(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``manager.stop()`` resets the snapshot to ``None`` (no leak across sessions)."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "fake"},
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"},
        },
        {"type": "result", "subtype": "success", "duration_ms": 10},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    await manager.new_session(prompt=None, started_by_tg_user_id=None)
    await _drain_status(bus, SessionStatus.COMPLETED)
    await manager.stop()

    assert manager.current_rate_limit_status() is None
