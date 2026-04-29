"""Globally enforces a single running Claude subprocess session.

Composition:
  - ``bus``: shared :class:`~ccr.events.EventBus` (Telegram + SSE consumers).
  - ``db_factory``: :class:`async_sessionmaker` for short-lived DB writes.
  - ``settings``: :class:`~ccr.config.Settings` for paths and timeouts.

Internal state is guarded by a single ``asyncio.Lock`` (``_session_lock``).
``new_session()`` holds the lock for the full transition: it stops any
prior session silently, starts a fresh subprocess, inserts the
:class:`~ccr.db.models.Session` row only after the subprocess starts
cleanly, and (if a prompt was given) waits for the ``system.init`` event
before sending the first user turn.

Ordering guarantee: ``_log.append()`` always completes before
``_bus.publish()`` for every event — SSE replay-then-tail relies on the
log being on disk before the bus fires.

Slim DB writes: every write opens a fresh session via the factory, commits
eagerly, and never holds it across an ``await``. ``last_event_at`` is
debounced to at most one update per second.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from ccr.claude.events import (
    PermissionRequest,
    ResultEvent,
    SystemInit,
    UnknownEvent,
)
from ccr.claude.log import JsonlSessionLog
from ccr.claude.process import ClaudeProcess
from ccr.claude.state import SessionStatus
from ccr.db.models import Session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ccr.config import Settings
    from ccr.events import EventBus


log = structlog.get_logger(__name__)


_FIRST_PROMPT_TRUNCATE = 500
_LAST_EVENT_DEBOUNCE_SECONDS = 1.0
_INIT_WAIT_TIMEOUT_SECONDS = 30.0
_CRASH_REASON_TAIL_BYTES = 200


class SessionError(Exception):
    """Base error for :class:`SessionManager` operations."""


class NoActiveSessionError(SessionError):
    """Raised when an operation requires a running session but none exists."""


class StaleSessionError(SessionError):
    """Raised when a permission response targets a session that is no longer current."""


class SessionManager:
    """Owns the global Claude subprocess and its companion log + bus."""

    def __init__(
        self,
        *,
        bus: EventBus,
        db_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._bus = bus
        self._db_factory = db_factory
        self._settings = settings
        self._logs_dir = settings.data_dir / "logs"

        self._session_lock = asyncio.Lock()
        self._proc: ClaudeProcess | None = None
        self._session_id: uuid.UUID | None = None
        self._started_at: datetime | None = None
        self._log: JsonlSessionLog | None = None
        self._consumer_task: asyncio.Task[None] | None = None
        self._exit_task: asyncio.Task[None] | None = None
        self._status: SessionStatus = SessionStatus.IDLE
        self._stop_requested = False

        # Per-session bookkeeping reset on each new_session().
        self._init_event: asyncio.Event = asyncio.Event()
        self._saw_result_success = False
        self._last_event_at_write_ts: float = 0.0
        self._last_event_at_pending: asyncio.Task[None] | None = None

        # CCR-009 — permission gating. ``request_id`` is the Claude-emitted
        # opaque id; ``session_id`` is the manager-owned UUID for the
        # subprocess session. The dicts persist across the lifetime of the
        # ``SessionManager`` because permission requests are scoped to a
        # single session and are cleaned up either on response or teardown.
        # Refcount the pause so multiple concurrent permission_request
        # events for the same session pause once and unpause when the last
        # response lands.
        self._pending_permissions: dict[str, asyncio.Event] = {}
        self._pending_options: dict[str, frozenset[str]] = {}
        self._telegram_pause_count: dict[uuid.UUID, int] = {}
        self._telegram_resume: dict[uuid.UUID, asyncio.Event] = {}

    # ------------------------------------------------------------------ #
    # Public API.
    # ------------------------------------------------------------------ #

    async def status(self) -> SessionStatus:
        """Return the current in-memory status; ``IDLE`` if no subprocess."""
        if self._proc is None:
            return SessionStatus.IDLE
        return self._status

    async def info(self) -> dict[str, object]:
        """Return snapshot metadata for the current session.

        Keys: ``session_id`` (``uuid.UUID | None``), ``pid`` (``int | None``),
        ``started_at`` (``datetime | None``), ``status``
        (:class:`SessionStatus`).
        """
        if self._proc is None:
            return {
                "session_id": None,
                "pid": None,
                "started_at": None,
                "status": SessionStatus.IDLE,
            }
        return {
            "session_id": self._session_id,
            "pid": self._proc.pid,
            "started_at": self._started_at,
            "status": self._status,
        }

    async def new_session(
        self,
        *,
        prompt: str | None,
        started_by_tg_user_id: int | None,
    ) -> uuid.UUID:
        """Start a fresh Claude session. Returns the new ``Session.id``.

        Stops any prior session silently. The :class:`Session` row is only
        inserted after :meth:`ClaudeProcess.start` returns cleanly. If a
        prompt is provided, we wait for the ``system.init`` event before
        sending the user turn so Claude Code does not race the init with
        the first message.
        """
        async with self._session_lock:
            if self._proc is not None:
                await self._teardown_locked(final_status=SessionStatus.STOPPED)

            session_id = uuid.uuid4()
            log_path = self._logs_dir / f"{session_id}.jsonl"
            session_log = JsonlSessionLog(log_path)
            await session_log.open()

            proc = ClaudeProcess(settings=self._settings)
            try:
                await proc.start()
            except FileNotFoundError as exc:
                message = f"claude binary not found: {self._settings.claude_bin!r}"
                raise SessionError(message) from exc

            self._proc = proc
            self._session_id = session_id
            self._log = session_log
            self._status = SessionStatus.RUNNING
            self._stop_requested = False
            self._saw_result_success = False
            self._init_event = asyncio.Event()
            self._last_event_at_write_ts = 0.0
            self._last_event_at_pending = None

            now = datetime.now(UTC)
            self._started_at = now
            await self._db_insert_session(
                session_id=session_id,
                started_at=now,
                started_by_tg_user_id=started_by_tg_user_id,
                first_prompt=(prompt or None),
            )

            self._consumer_task = asyncio.create_task(
                self._consume_events(),
                name=f"claude-consumer-{session_id}",
            )
            self._exit_task = asyncio.create_task(
                self._await_exit(),
                name=f"claude-exit-{session_id}",
            )

            await self._bus.publish(
                "session.status",
                {
                    "session_id": session_id,
                    "status": SessionStatus.RUNNING,
                    "ts": now,
                },
            )

            if prompt:
                try:
                    await asyncio.wait_for(
                        self._init_event.wait(),
                        timeout=_INIT_WAIT_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    log.warning(
                        "session_manager.init_timeout",
                        session_id=str(session_id),
                    )
                # The subprocess may have crashed during init; only send if
                # we still hold a running process.
                if self._proc is proc and self._status == SessionStatus.RUNNING:
                    try:
                        await proc.send_user_turn(prompt)
                    except RuntimeError as exc:
                        log.warning(
                            "session_manager.send_user_turn_failed",
                            session_id=str(session_id),
                            error=str(exc),
                        )

            return session_id

    async def send(self, prompt: str) -> None:
        """Forward ``prompt`` as a user turn to the running session."""
        if self._proc is None or self._status != SessionStatus.RUNNING:
            message = "No active session."
            raise NoActiveSessionError(message)
        await self._proc.send_user_turn(prompt)

    async def send_permission(
        self,
        session_id: uuid.UUID,
        request_id: str,
        choice: str,
    ) -> None:
        """Forward a permission response. Validates the session id matches."""
        if self._proc is None or self._session_id is None:
            message = "No active session."
            raise NoActiveSessionError(message)
        if session_id != self._session_id:
            message = (
                f"Permission target {session_id} does not match active session {self._session_id}."
            )
            raise StaleSessionError(message)
        await self._proc.send_permission_response(request_id, choice)
        self._clear_pending_permission(session_id, request_id)

    def is_telegram_paused(self, session_id: uuid.UUID) -> bool:
        """Return True iff at least one permission_request is outstanding for this session.

        Consulted by :func:`ccr.server._broadcast_loop` to decide whether
        to buffer non-permission events. The flag is independent of SSE —
        SSE consumers subscribe to the same bus and ignore this gate.
        """
        return self._telegram_pause_count.get(session_id, 0) > 0

    async def wait_for_resume(self, session_id: uuid.UUID) -> None:
        """Block until ``session_id`` has no outstanding permission_request.

        Currently unused by the broadcast loop (which polls
        :meth:`is_telegram_paused` per event to avoid stalling
        ``bus.subscribe`` consumption), but kept as a primitive for future
        consumers that need an awaitable handle.
        """
        ev = self._telegram_resume.get(session_id)
        if ev is None or ev.is_set():
            return
        await ev.wait()

    def is_permission_choice_valid(self, request_id: str, choice: str) -> bool:
        """Validate ``choice`` against the option set captured at request time.

        ``callback_data`` is forgeable; the manager keeps the authoritative
        ``options`` snapshot from the original
        :class:`~ccr.claude.events.PermissionRequest` event. Returns False
        for unknown ``request_id`` (already-answered or torn-down session)
        as well as for unrecognised ``choice`` values.
        """
        opts = self._pending_options.get(request_id)
        return opts is not None and choice in opts

    def _clear_pending_permission(
        self,
        session_id: uuid.UUID,
        request_id: str,
    ) -> None:
        """Drop ``request_id`` from the pending dicts and decrement the pause counter.

        Idempotent — calling this for an already-cleared ``request_id`` is a
        no-op. The per-``request_id`` event is fired on first clear so any
        :meth:`wait_for_resume` waiter wakes; the per-``session_id`` resume
        Event flips only when the counter hits 0 (handles concurrent
        permission_requests for the same session).
        """
        self._pending_options.pop(request_id, None)
        ev = self._pending_permissions.pop(request_id, None)
        if ev is not None:
            ev.set()
        count = self._telegram_pause_count.get(session_id, 0)
        if count <= 1:
            self._telegram_pause_count.pop(session_id, None)
            resume = self._telegram_resume.get(session_id)
            if resume is not None:
                resume.set()
        else:
            self._telegram_pause_count[session_id] = count - 1

    async def stop(self) -> None:
        """Idempotent shutdown: SIGTERM → grace → SIGKILL → STOPPED."""
        async with self._session_lock:
            if self._proc is None:
                log.debug("session_manager.stop_noop")
                return
            await self._teardown_locked(final_status=SessionStatus.STOPPED)

    # ------------------------------------------------------------------ #
    # Internals.
    # ------------------------------------------------------------------ #

    async def _teardown_locked(self, *, final_status: SessionStatus) -> None:
        """Tear the current session down. Caller holds ``_session_lock``."""
        proc = self._proc
        session_id = self._session_id
        consumer = self._consumer_task
        exit_task = self._exit_task

        if proc is None or session_id is None:
            return

        self._stop_requested = final_status == SessionStatus.STOPPED

        # CCR-009 — clear permission-gate state for the dying session before
        # we drop the references. Wake any orphaned wait_for_resume waiters
        # first so they unblock cleanly. Keys for OTHER sessions (currently
        # impossible — one session at a time — but cheap to be correct) are
        # left intact.
        resume = self._telegram_resume.pop(session_id, None)
        if resume is not None:
            resume.set()
        self._telegram_pause_count.pop(session_id, None)
        # Drop every pending request_id we recorded; we do not track which
        # request_id belongs to which session so we walk both dicts. In
        # practice _pending_options and _pending_permissions only contain
        # entries for the session being torn down (one session globally).
        for ev in list(self._pending_permissions.values()):
            ev.set()
        self._pending_permissions.clear()
        self._pending_options.clear()

        await proc.stop()

        if exit_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await exit_task
        if consumer is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await consumer

        # Final status is decided by _await_exit (it inspects _stop_requested,
        # exit code, _saw_result_success). Re-read self._status here in case
        # _await_exit already decided CRASHED before stop() reached this
        # point.
        if final_status == SessionStatus.STOPPED:
            self._status = SessionStatus.STOPPED

        # Make sure the in-flight last_event_at write completes before we
        # blow away references.
        if self._last_event_at_pending is not None:
            with contextlib.suppress(BaseException):
                await self._last_event_at_pending

        self._proc = None
        self._session_id = None
        self._started_at = None
        self._log = None
        self._consumer_task = None
        self._exit_task = None

    async def _consume_events(self) -> None:
        """Drain :meth:`ClaudeProcess.events` into the log + bus.

        Ordering for non-permission events: ``log.append`` → ``bus.publish``.

        Ordering for :class:`~ccr.claude.events.PermissionRequest` events
        (CCR-009): ``log.append`` → permission bookkeeping (capture options,
        bump pause counter) → ``bus.publish``. The bookkeeping runs BEFORE
        the bus publish so any subscriber that consults
        :meth:`is_telegram_paused` for follow-up events sees an already-set
        gate. The PermissionRequest message itself bypasses the gate in
        :func:`ccr.server._broadcast_loop` (it carries the keyboard the
        user needs to tap), so the order does not stall the prompt itself.
        """
        proc = self._proc
        log_obj = self._log
        session_id = self._session_id
        if proc is None or log_obj is None or session_id is None:
            return  # pragma: no cover — defensive; only called after new_session

        try:
            async for event in proc.events():
                seq = await log_obj.append(event)

                if isinstance(event, PermissionRequest):
                    self._record_pending_permission(session_id, event)

                await self._bus.publish(
                    "session.event",
                    {"session_id": session_id, "seq": seq, "event": event},
                )

                if isinstance(event, SystemInit):
                    self._init_event.set()
                elif isinstance(event, ResultEvent) and event.subtype == "success":
                    self._saw_result_success = True

                self._schedule_last_event_at_update(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "session_manager.consumer_error",
                session_id=str(session_id),
            )

    def _record_pending_permission(
        self,
        session_id: uuid.UUID,
        event: PermissionRequest,
    ) -> None:
        """Capture options + bump the pause counter for ``event.request_id``."""
        self._pending_options[event.request_id] = frozenset(event.options)
        self._pending_permissions[event.request_id] = asyncio.Event()
        self._telegram_pause_count[session_id] = self._telegram_pause_count.get(session_id, 0) + 1
        resume = self._telegram_resume.get(session_id)
        if resume is None:
            resume = asyncio.Event()
            self._telegram_resume[session_id] = resume
        resume.clear()

    def _schedule_last_event_at_update(self, session_id: uuid.UUID) -> None:
        """Debounce ``last_event_at`` writes to ≤ 1 per second, fire-and-forget."""
        now_ts = time.monotonic()
        # First call always fires; ``_last_event_at_write_ts == 0.0`` is the
        # "never written" sentinel and must not be subtracted into the
        # debounce window.
        if (
            self._last_event_at_write_ts > 0.0
            and now_ts - self._last_event_at_write_ts < _LAST_EVENT_DEBOUNCE_SECONDS
        ):
            return
        self._last_event_at_write_ts = now_ts
        self._last_event_at_pending = asyncio.create_task(
            self._update_last_event_at(session_id),
            name=f"claude-last-event-{session_id}",
        )

    async def _update_last_event_at(self, session_id: uuid.UUID) -> None:
        """Fire-and-forget update of ``Session.last_event_at``."""
        try:
            async with self._db_factory() as db:
                row = await db.scalar(select(Session).where(Session.id == session_id))
                if row is None:
                    return
                row.last_event_at = datetime.now(UTC)
                await db.commit()
        except Exception:
            log.exception(
                "session_manager.last_event_at_update_failed",
                session_id=str(session_id),
            )

    async def _await_exit(self) -> None:
        """Wait for the subprocess to exit; decide final status."""
        proc = self._proc
        log_obj = self._log
        session_id = self._session_id
        if proc is None or log_obj is None or session_id is None:
            return  # pragma: no cover — defensive

        exit_code = await proc.wait()

        # Wait for the consumer to drain remaining stdout (it sees EOF).
        if self._consumer_task is not None and self._consumer_task is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._consumer_task

        # Drain any in-flight ``last_event_at`` write before we issue the
        # final-state update — otherwise observers that read the row right
        # after seeing the COMPLETED/CRASHED status may still see
        # ``last_event_at`` as NULL.
        if self._last_event_at_pending is not None:
            with contextlib.suppress(BaseException):
                await self._last_event_at_pending

        if self._stop_requested:
            final_status = SessionStatus.STOPPED
            exit_reason: str | None = "stopped"
        elif exit_code == 0 and self._saw_result_success:
            final_status = SessionStatus.COMPLETED
            exit_reason = "completed"
        else:
            final_status = SessionStatus.CRASHED
            exit_reason = self._build_crash_reason(exit_code, proc.stderr_tail)
            crash_event = UnknownEvent(
                type="error",
                raw={
                    "reason": exit_reason,
                    "exit_code": exit_code,
                    "stderr_tail": proc.stderr_tail.decode("utf-8", errors="replace"),
                },
            )
            seq = await log_obj.append(crash_event)
            await self._bus.publish(
                "session.event",
                {"session_id": session_id, "seq": seq, "event": crash_event},
            )

        self._status = final_status

        await self._db_finalize_session(
            session_id=session_id,
            status=final_status,
            ended_at=datetime.now(UTC),
            exit_reason=exit_reason,
        )

        await self._bus.publish(
            "session.status",
            {
                "session_id": session_id,
                "status": final_status,
                "ts": datetime.now(UTC),
            },
        )

    @staticmethod
    def _build_crash_reason(exit_code: int, stderr_tail: bytes) -> str:
        tail = stderr_tail.decode("utf-8", errors="replace").strip()
        if tail:
            short = (
                tail[-_CRASH_REASON_TAIL_BYTES:] if len(tail) > _CRASH_REASON_TAIL_BYTES else tail
            )
            return f"exit={exit_code} stderr_tail={short!r}"
        return f"exit={exit_code}"

    # ------------------------------------------------------------------ #
    # DB helpers — short-lived sessions, eager commits.
    # ------------------------------------------------------------------ #

    async def _db_insert_session(
        self,
        *,
        session_id: uuid.UUID,
        started_at: datetime,
        started_by_tg_user_id: int | None,
        first_prompt: str | None,
    ) -> None:
        truncated_prompt: str | None
        if first_prompt is not None:
            truncated_prompt = first_prompt[:_FIRST_PROMPT_TRUNCATE]
        else:
            truncated_prompt = None
        try:
            async with self._db_factory() as db:
                row = Session(
                    id=session_id,
                    started_at=started_at,
                    status=SessionStatus.RUNNING.value,
                    started_by_tg_user_id=started_by_tg_user_id,
                    first_prompt=truncated_prompt,
                )
                db.add(row)
                await db.commit()
        except Exception:
            log.exception(
                "session_manager.session_insert_failed",
                session_id=str(session_id),
            )

    async def _db_finalize_session(
        self,
        *,
        session_id: uuid.UUID,
        status: SessionStatus,
        ended_at: datetime,
        exit_reason: str | None,
    ) -> None:
        if status == SessionStatus.IDLE:  # pragma: no cover — guard
            message = "IDLE is in-memory only and must not be written to Session.status."
            raise ValueError(message)
        try:
            async with self._db_factory() as db:
                row = await db.scalar(select(Session).where(Session.id == session_id))
                if row is None:
                    return
                row.status = status.value
                row.ended_at = ended_at
                row.exit_reason = exit_reason
                await db.commit()
        except Exception:
            log.exception(
                "session_manager.session_finalize_failed",
                session_id=str(session_id),
            )

    # ------------------------------------------------------------------ #
    # Test/debug accessors (read-only).
    # ------------------------------------------------------------------ #

    @property
    def current_session_id(self) -> uuid.UUID | None:
        """Currently running session's ``Session.id`` or ``None``."""
        return self._session_id

    @property
    def current_log(self) -> JsonlSessionLog | None:
        """Currently running session's :class:`JsonlSessionLog` or ``None``."""
        return self._log


__all__ = [
    "NoActiveSessionError",
    "SessionError",
    "SessionManager",
    "StaleSessionError",
]
