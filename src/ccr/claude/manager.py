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
    ResultEvent,
    SystemInit,
    UnknownEvent,
)
from ccr.claude.log import JsonlSessionLog
from ccr.claude.mcp import McpPermissionServer, McpServerStartError
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


class SessionAlreadyRunningError(SessionError):
    """Raised when :meth:`SessionManager.continue_session` is called while a session is running."""


class NoPriorSessionError(SessionError):
    """Raised when :meth:`SessionManager.continue_session` finds no eligible prior session."""


class SessionNotFoundError(SessionError):
    """Raised when ``continue_session`` is given a prefix that matches no resumable row."""


# Status set defining "finished and resumable". ``crashed`` is deliberately
# excluded — a crashed session may have left Claude's local conversation
# cache in a partial state and resuming is unsafe. ``running`` is excluded
# by definition (CCR-019 reconciliation guarantees ``status='running'`` rows
# reflect a live process at startup, and ``continue_session`` already
# refuses to act if a process is currently held).
_RESUMABLE_STATUSES: tuple[str, ...] = (
    SessionStatus.COMPLETED.value,
    SessionStatus.STOPPED.value,
)


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

        # MCP permission gate (CCR-025). Lifetime = manager lifetime; lazily
        # started on the first new_session / continue_session call so a
        # SessionManager constructed in tests that never spins claude does
        # not pay the listener cost.
        self._mcp = McpPermissionServer(
            bus=bus,
            timeout_seconds=float(settings.mcp_permission_timeout_seconds),
            data_dir=settings.data_dir,
        )
        self._mcp_started = False

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

            await self._ensure_mcp_started()

            session_id = uuid.uuid4()
            log_path = self._logs_dir / f"{session_id}.jsonl"
            session_log = JsonlSessionLog(log_path)
            await session_log.open()

            proc = ClaudeProcess(settings=self._settings, mcp_argv=self._mcp.claude_argv)
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
            self._mcp.set_current_session(session_id)

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

    async def continue_session(
        self,
        *,
        started_by_tg_user_id: int | None,
        session_id_prefix: str | None = None,
    ) -> uuid.UUID:
        """Resume a finished session. Returns the new ``Session.id``.

        With no ``session_id_prefix``, the most recent finished session is
        chosen. With a prefix, the matching row from the resumable set is
        chosen (most recent on the vanishingly rare 8-hex collision).

        Refuses to silently replace a running session — caller must
        ``/stop`` or ``/clear`` first. Mints a fresh local UUID and inserts
        a new :class:`Session` row; spawns a fresh :class:`ClaudeProcess`
        with ``--continue`` so Claude Code restores conversation context
        from its own local cache.

        Raises:
            SessionAlreadyRunningError: a session is currently running.
            NoPriorSessionError: no completed/stopped row exists in the DB
                (no-arg path only).
            SessionNotFoundError: a ``session_id_prefix`` was given but no
                resumable row's first 8 hex chars match.
            SessionError: ``claude_bin`` is missing or the subprocess
                otherwise fails to start (mirrors :meth:`new_session`).
        """
        async with self._session_lock:
            if self._proc is not None:
                message = "Session already running. /stop first or /clear to start fresh."
                raise SessionAlreadyRunningError(message)

            if session_id_prefix is None:
                prior_id, _prior_claude_id = await self._db_lookup_most_recent_finished()
                if prior_id is None:
                    message = "No prior session to continue."
                    raise NoPriorSessionError(message)
            else:
                prior_id, _prior_claude_id = await self._db_lookup_session_by_prefix(
                    session_id_prefix,
                )
                if prior_id is None:
                    message = f"No session found with id {session_id_prefix}."
                    raise SessionNotFoundError(message)

            await self._ensure_mcp_started()

            session_id = uuid.uuid4()
            log_path = self._logs_dir / f"{session_id}.jsonl"
            session_log = JsonlSessionLog(log_path)
            await session_log.open()

            proc = ClaudeProcess(
                settings=self._settings,
                resume=True,
                mcp_argv=self._mcp.claude_argv,
            )
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
            self._mcp.set_current_session(session_id)

            now = datetime.now(UTC)
            self._started_at = now
            await self._db_insert_session(
                session_id=session_id,
                started_at=now,
                started_by_tg_user_id=started_by_tg_user_id,
                first_prompt=None,
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

            return session_id

    async def send(self, prompt: str) -> None:
        """Forward ``prompt`` as a user turn to the running session."""
        if self._proc is None or self._status != SessionStatus.RUNNING:
            message = "No active session."
            raise NoActiveSessionError(message)
        await self._proc.send_user_turn(prompt)

    async def send_slash(self, name: str, args: str) -> None:
        """Forward ``/name args`` as a user turn — Claude Code handles it as a slash command."""
        prompt = f"/{name} {args}".rstrip()
        await self.send(prompt)

    async def reconcile_orphans(self) -> int:
        """Mark every DB row stuck in ``status='running'`` as ``crashed``.

        On bot crash / kill the child Claude subprocess dies with the parent
        but the :class:`Session` row stays ``status='running'`` forever
        because :meth:`_db_finalize_session` never runs. After restart the DB
        lies about live sessions — call this once during startup to repair
        the lie.

        Returns the number of rows reconciled. Idempotent: a second call on
        an already-clean DB returns ``0``. Logs one structured line per row
        so operators can audit the cleanup.
        """
        reconciled = 0
        now = datetime.now(UTC)
        async with self._db_factory() as db:
            rows = (
                await db.scalars(
                    select(Session).where(Session.status == SessionStatus.RUNNING.value),
                )
            ).all()
            for row in rows:
                row.status = SessionStatus.CRASHED.value
                row.exit_reason = "bot restart"
                row.ended_at = now
                log.info(
                    "session_manager.orphan_reconciled",
                    session_id=str(row.id),
                    started_at=str(row.started_at),
                )
                reconciled += 1
            if reconciled:
                await db.commit()
        return reconciled

    async def stop(self) -> None:
        """Idempotent shutdown: SIGTERM → grace → SIGKILL → STOPPED."""
        async with self._session_lock:
            if self._proc is None:
                log.debug("session_manager.stop_noop")
                return
            await self._teardown_locked(final_status=SessionStatus.STOPPED)

    async def shutdown(self) -> None:
        """Stop the active session AND tear the MCP permission server down.

        Called from :func:`ccr.server.serve`'s ``finally`` block. Idempotent.
        """
        await self.stop()
        if self._mcp_started:
            await self._mcp.stop()
            self._mcp_started = False

    async def resolve_permission(
        self,
        request_id: str,
        decision: dict[str, object],
    ) -> bool:
        """Forward to :meth:`McpPermissionServer.resolve`.

        Returns ``True`` if the Future was set by THIS call, ``False`` if
        ``request_id`` was unknown or already resolved (the bot maps both
        to "Stale prompt").
        """
        return await self._mcp.resolve(request_id, dict(decision))

    def is_permission_pending(self, request_id: str) -> bool:
        """Return ``True`` iff a Future is registered for ``request_id``."""
        return self._mcp.is_pending(request_id)

    # ------------------------------------------------------------------ #
    # Internals.
    # ------------------------------------------------------------------ #

    async def _ensure_mcp_started(self) -> None:
        """Lazily start the MCP permission server (idempotent).

        Mirrors the ``FileNotFoundError`` → :class:`SessionError` mapping
        used for the claude binary so callers see one error type.
        """
        if self._mcp_started:
            return
        try:
            await self._mcp.start()
        except McpServerStartError as exc:
            message = f"failed to start MCP permission server: {exc}"
            raise SessionError(message) from exc
        self._mcp_started = True

    async def _teardown_locked(self, *, final_status: SessionStatus) -> None:
        """Tear the current session down. Caller holds ``_session_lock``."""
        proc = self._proc
        session_id = self._session_id
        consumer = self._consumer_task
        exit_task = self._exit_task

        if proc is None or session_id is None:
            return

        self._stop_requested = final_status == SessionStatus.STOPPED

        # Resolve any outstanding permission Futures with deny BEFORE we
        # send SIGTERM so claude's tool dispatch sees a clean deny rather
        # than a hung MCP socket.
        await self._mcp.cancel_pending(session_id)
        self._mcp.set_current_session(None)

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

        Ordering: ``log.append`` → ``bus.publish`` for every event so SSE
        replay-then-tail subscribers always find the event on disk before
        the bus fires.
        """
        proc = self._proc
        log_obj = self._log
        session_id = self._session_id
        if proc is None or log_obj is None or session_id is None:
            return  # pragma: no cover — defensive; only called after new_session

        try:
            async for event in proc.events():
                seq = await log_obj.append(event)

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

    async def _db_lookup_most_recent_finished(
        self,
    ) -> tuple[uuid.UUID | None, str | None]:
        """Return ``(id, claude_session_id)`` for the most recent finished session.

        "Finished" = ``status IN ('completed', 'stopped')``; ``crashed`` is
        deliberately excluded (see :data:`_RESUMABLE_STATUSES`). The second
        tuple element is reserved for the Outcome 2 ``claude_session_id``
        capture path; in Outcome 1 (the path landed by CCR-020) the column
        does not exist on the row and the value is always ``None``.
        """
        async with self._db_factory() as db:
            row = await db.scalar(
                select(Session)
                .where(Session.status.in_(_RESUMABLE_STATUSES))
                .order_by(Session.started_at.desc())
                .limit(1),
            )
            if row is None:
                return None, None
            return row.id, getattr(row, "claude_session_id", None)

    async def _db_lookup_session_by_prefix(
        self,
        prefix: str,
    ) -> tuple[uuid.UUID | None, str | None]:
        """Return ``(id, claude_session_id)`` for a resumable row matching the 8-hex prefix.

        Same eligibility set as :meth:`_db_lookup_most_recent_finished`
        (``crashed`` and ``running`` excluded). The match runs in Python on
        ``row.id.hex[:8]`` rather than SQL because :class:`Session.id` is
        stored as a backend-specific UUID type (binary on most backends,
        TEXT on SQLite) and a portable LIKE/SUBSTR predicate would have to
        cast both sides; the resumable result set is small (worst case a
        few hundred entries) so the in-Python filter is acceptable. On the
        vanishingly rare 8-hex collision the most recent ``started_at``
        wins (the rows are scanned in DESC order).
        """
        async with self._db_factory() as db:
            rows = (
                await db.scalars(
                    select(Session)
                    .where(Session.status.in_(_RESUMABLE_STATUSES))
                    .order_by(Session.started_at.desc()),
                )
            ).all()
            for row in rows:
                if row.id.hex[:8] == prefix:
                    return row.id, getattr(row, "claude_session_id", None)
            return None, None

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
    "NoPriorSessionError",
    "SessionAlreadyRunningError",
    "SessionError",
    "SessionManager",
    "SessionNotFoundError",
    "StaleSessionError",
]
