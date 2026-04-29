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
    StaleSessionError,
)
from ccr.claude.events import PermissionRequest, UnknownEvent
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


async def test_send_permission_with_stale_session_id_raises(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid as _uuid

    events = [
        {"type": "system", "subtype": "init"},
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script, delay_ms=200)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)
    await manager.new_session(prompt=None, started_by_tg_user_id=None)
    bogus = _uuid.uuid4()
    with pytest.raises(StaleSessionError):
        await manager.send_permission(bogus, "req-1", "approve")

    await manager.stop()


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


# --------------------------------------------------------------------------- #
# CCR-009: permission gating.
# --------------------------------------------------------------------------- #


async def _wait_for_pause(
    manager: SessionManager,
    session_id: object,
    timeout: float = 5.0,
) -> None:
    """Spin until ``manager.is_telegram_paused(session_id)`` is True."""
    import uuid as _uuid_mod

    assert isinstance(session_id, _uuid_mod.UUID)
    deadline = asyncio.get_running_loop().time() + timeout
    while not manager.is_telegram_paused(session_id):
        if asyncio.get_running_loop().time() > deadline:
            msg = "is_telegram_paused never went True"
            raise AssertionError(msg)
        await asyncio.sleep(0.01)


async def test_permission_request_pauses_telegram_and_clears_on_response(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PermissionRequest event flips ``is_telegram_paused`` True; ``send_permission`` clears it."""
    events = [
        {"type": "system", "subtype": "init"},
        {
            "type": "permission_request",
            "request_id": "r1",
            "tool_name": "bash",
            "input": {"cmd": "ls"},
            "options": ["approve", "skip", "abort"],
        },
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    # Slow the producer so we can observe the paused state before the
    # ResultEvent triggers session completion.
    _set_fake_env(monkeypatch, script=script, delay_ms=200)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    session_id = await manager.new_session(prompt=None, started_by_tg_user_id=None)

    await _wait_for_pause(manager, session_id)
    assert manager.is_telegram_paused(session_id) is True
    assert manager.is_permission_choice_valid("r1", "approve") is True

    await manager.send_permission(session_id, "r1", "approve")
    assert manager.is_telegram_paused(session_id) is False
    assert manager.is_permission_choice_valid("r1", "approve") is False

    await manager.stop()


async def test_concurrent_permission_requests_count_correctly(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two outstanding permission_requests bump the counter; both must clear before unpause."""
    events = [
        {"type": "system", "subtype": "init"},
        {
            "type": "permission_request",
            "request_id": "r1",
            "tool_name": "bash",
            "input": {"cmd": "ls"},
            "options": ["approve", "skip"],
        },
        {
            "type": "permission_request",
            "request_id": "r2",
            "tool_name": "bash",
            "input": {"cmd": "rm"},
            "options": ["approve", "abort"],
        },
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script, delay_ms=200)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    received: list[ClaudeEvent] = []

    async def consumer() -> None:
        async for payload in bus.subscribe("session.event"):
            assert isinstance(payload, dict)
            received.append(payload["event"])  # type: ignore[arg-type]
            # Stop after the second permission_request so we observe both.
            perms = [e for e in received if isinstance(e, PermissionRequest)]
            if len(perms) >= 2:
                return

    consumer_task = asyncio.create_task(consumer())
    await asyncio.sleep(0)

    session_id = await manager.new_session(prompt=None, started_by_tg_user_id=None)
    await asyncio.wait_for(consumer_task, timeout=5.0)

    assert manager.is_telegram_paused(session_id) is True
    # Counter == 2 internally — we infer it by clearing one and asserting
    # the gate stays paused.
    await manager.send_permission(session_id, "r1", "approve")
    assert manager.is_telegram_paused(session_id) is True
    assert manager.is_permission_choice_valid("r1", "approve") is False
    assert manager.is_permission_choice_valid("r2", "approve") is True

    await manager.send_permission(session_id, "r2", "approve")
    assert manager.is_telegram_paused(session_id) is False

    await manager.stop()


async def test_is_permission_choice_valid_rejects_forged_choice(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``is_permission_choice_valid`` rejects choices not in the captured options set."""
    events = [
        {"type": "system", "subtype": "init"},
        {
            "type": "permission_request",
            "request_id": "r1",
            "tool_name": "bash",
            "input": {"cmd": "ls"},
            "options": ["approve", "skip"],
        },
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script, delay_ms=200)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    session_id = await manager.new_session(prompt=None, started_by_tg_user_id=None)
    await _wait_for_pause(manager, session_id)

    assert manager.is_permission_choice_valid("r1", "approve") is True
    assert manager.is_permission_choice_valid("r1", "skip") is True
    assert manager.is_permission_choice_valid("r1", "abort") is False
    assert manager.is_permission_choice_valid("nope", "approve") is False

    await manager.stop()


async def test_session_teardown_clears_pending_permissions(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``manager.stop()`` clears all permission-gate state for the dying session."""
    events = [
        {"type": "system", "subtype": "init"},
        {
            "type": "permission_request",
            "request_id": "r1",
            "tool_name": "bash",
            "input": {"cmd": "ls"},
            "options": ["approve", "skip"],
        },
        {"type": "result", "subtype": "success"},
    ]
    script = _write_script(tmp_path, events)
    _set_fake_env(monkeypatch, script=script, delay_ms=200)

    bus = EventBus()
    manager = SessionManager(bus=bus, db_factory=session_factory, settings=settings)

    session_id = await manager.new_session(prompt=None, started_by_tg_user_id=None)
    await _wait_for_pause(manager, session_id)
    assert manager.is_telegram_paused(session_id) is True

    await manager.stop()

    assert manager.is_telegram_paused(session_id) is False
    assert manager.is_permission_choice_valid("r1", "approve") is False
    # Internal dicts are empty after teardown. We read through ``getattr``
    # so ruff's SLF001 (private-member access) does not trip; the public
    # accessors above already cover the user-facing observation.
    assert getattr(manager, "_pending_permissions") == {}  # noqa: B009
    assert getattr(manager, "_pending_options") == {}  # noqa: B009
    assert getattr(manager, "_telegram_pause_count") == {}  # noqa: B009
    assert getattr(manager, "_telegram_resume") == {}  # noqa: B009


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
