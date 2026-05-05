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
import collections
import contextlib
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from ccr.claude.events import (
    AssistantTurn,
    RateLimitEvent,
    ResultEvent,
    SystemInit,
    ToolResultBlock,
    ToolUseBlock,
    UnknownEvent,
    UserTurn,
)
from ccr.claude.log import JsonlSessionLog
from ccr.claude.mcp import McpPermissionServer, McpServerStartError
from ccr.claude.process import ClaudeProcess
from ccr.claude.state import SessionStatus
from ccr.claude.usage import SessionUsage, aggregate_session_usage
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

# Tool names that mark a subagent dispatch in stream-json. ``Task`` is the
# modern name (per ``SystemInit.tools`` in v2.1.123 logs); ``Agent`` appears
# in older session logs. Both are accepted to be forward/backward compatible.
_SUBAGENT_DISPATCH_TOOL_NAMES: frozenset[str] = frozenset({"Task", "Agent"})

# CCR-026: name of the built-in tool whose tool_use blocks we capture as
# interactive Telegram prompts. Tracked separately from
# ``_SUBAGENT_DISPATCH_TOOL_NAMES`` — different lifecycle.
_ASK_USER_QUESTION_TOOL_NAME = "AskUserQuestion"


def _extract_ask_user_question_options(tool_input: dict[str, object]) -> list[str]:
    """Pull discrete option labels out of an ``AskUserQuestion`` ``tool_input``.

    The probed wire schema (CCR-026 Step-0) is::

        {
          "questions": [
            {"question": "...", "header": "...", "multiSelect": false,
             "options": [{"label": "red", "description": "..."}, ...]}
          ]
        }

    Multiple ``questions`` are coalesced into a single decision: the bot
    presents one keyboard / one prompt per ``tool_use_id`` so we expose the
    options of the FIRST question. If the first question has no options
    (or carries an empty list), this returns ``[]`` and the bot renders the
    free-text path. Tolerates schema drift: anything that does not match
    the probed shape — including the legacy guess of a flat ``options:
    list[str]`` — falls through to the appropriate branch.
    """
    questions = tool_input.get("questions")
    if isinstance(questions, list) and questions:
        first = questions[0]
        if isinstance(first, dict):
            opts = first.get("options")
            if isinstance(opts, list):
                labels: list[str] = []
                for entry in opts:
                    if isinstance(entry, dict):
                        label = entry.get("label")
                        if isinstance(label, str) and label:
                            labels.append(label)
                    elif isinstance(entry, str) and entry:
                        labels.append(entry)
                return labels
    # Fallback: flat ``options: list[str]`` (the original plan guess) /
    # forward-compat with future schema changes.
    flat = tool_input.get("options")
    if isinstance(flat, list):
        return [str(opt) for opt in flat if isinstance(opt, str) and opt]
    return []


@dataclass(slots=True)
class _PendingQuestion:
    """Per-question bookkeeping for an outstanding ``AskUserQuestion`` call.

    Lifetime spans from the ``tool_use`` block being observed in
    :meth:`SessionManager._consume_events` until either:

    * a paired user replies (button tap, ``/answer``, or plain text) and
      :meth:`SessionManager.send_tool_result` pops the entry; OR
    * the timeout task fires and pops the entry; OR
    * :meth:`SessionManager._teardown_locked` clears the dict.

    ``options`` is the list of option labels (extracted from the probed
    ``tool_input.questions[0].options[*].label`` schema). Empty list ⇒
    free-text question. ``timeout_task`` is the ``asyncio.Task`` scheduled
    to fire after ``settings.ask_user_question_timeout_seconds``; cancelled
    on resolution and on teardown.

    CCR-028: ``mcp_request_id`` is set when this AUQ tool_use was paired
    with an MCP ``--permission-prompt-tool`` invocation. When non-``None``,
    :meth:`SessionManager.send_tool_result` resolves the MCP Future with
    ``{"behavior": "deny", "message": <answer>}`` instead of writing a
    synthetic ``tool_result`` to claude's stdin (claude's harness writes
    the synthetic ``tool_result`` itself once the deny lands, and a
    parallel ``send_user_turn`` would race it). For MCP-paired entries
    the caller's ``is_error`` flag is ignored — claude's harness flags
    every deny as ``is_error=True`` on the wire.
    """

    tool_use_id: str
    session_id: uuid.UUID
    options: list[str]
    timeout_task: asyncio.Task[None] | None = None
    mcp_request_id: str | None = None


class SessionError(Exception):
    """Base error for :class:`SessionManager` operations."""


class NoActiveSessionError(SessionError):
    """Raised when an operation requires a running session but none exists."""


class StaleSessionError(SessionError):
    """Raised when a permission response targets a session that is no longer current."""


class StaleToolUseError(SessionError):
    """Raised when a tool_use_id is not registered in :attr:`_pending_questions`.

    Reserved for internal assertions and future callers that prefer an
    exception path. :meth:`SessionManager.send_tool_result` does NOT raise
    this — it returns ``False`` for unknown ids, mirroring the
    :meth:`SessionManager.resolve_permission` contract.
    """


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
        # tool_use_id -> subagent_type. Populated when a Task/Agent tool fires;
        # entry removed when the matching tool_result arrives.
        self._running_subagents: dict[str, str] = {}
        # Snapshot of skill names from the most recent system/init event.
        self._skills: list[str] = []
        # Most recent rate_limit_event snapshot. ``None`` until the first
        # event arrives; reset to ``None`` on every session boundary.
        self._rate_limit_status: RateLimitEvent | None = None

        # MCP permission gate (CCR-025). Lifetime = manager lifetime; lazily
        # started on the first new_session / continue_session call so a
        # SessionManager constructed in tests that never spins claude does
        # not pay the listener cost.
        #
        # CCR-028: ``AskUserQuestion`` is routed by claude through the same
        # ``--permission-prompt-tool`` channel as regular permissions, so
        # we suppress its bus envelope and route the MCP call to the
        # AUQ-specific handler that pairs it with ``_pending_questions``.
        self._mcp = McpPermissionServer(
            bus=bus,
            timeout_seconds=float(settings.mcp_permission_timeout_seconds),
            data_dir=settings.data_dir,
            suppressed_tool_names=frozenset({_ASK_USER_QUESTION_TOOL_NAME}),
        )
        self._mcp.set_suppressed_tool_handler(self._on_mcp_ask_user_question)
        self._mcp_started = False

        # CCR-026 AskUserQuestion bookkeeping. Lifetime = current session;
        # cleared in _teardown_locked. Maps ``tool_use_id`` (the full,
        # opaque id supplied by Claude) to the per-question record.
        self._pending_questions: dict[str, _PendingQuestion] = {}

        # CCR-028: AUQ MCP request_ids that arrived BEFORE the matching
        # ``AssistantTurn`` JSONL line registered ``_pending_questions``.
        # FIFO-drained from :meth:`_track_ask_user_question` when a fresh
        # question is registered. In observed real-claude runs the
        # AssistantTurn always lands first so this deque is effectively
        # empty in practice; defensive against stream reordering.
        self._unpaired_mcp_auq_calls: collections.deque[str] = collections.deque()

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
            self._running_subagents = {}
            self._skills = []
            self._rate_limit_status = None
            self._pending_questions = {}
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
            self._running_subagents = {}
            self._skills = []
            self._rate_limit_status = None
            self._pending_questions = {}
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

    async def _on_mcp_ask_user_question(
        self,
        request_id: str,
        tool_name: str,  # noqa: ARG002 — kept for handler symmetry
        tool_input: dict[str, Any],  # noqa: ARG002 — kept for handler symmetry
    ) -> None:
        """Pair an incoming AUQ MCP call with a :class:`_PendingQuestion` entry.

        The :class:`AssistantTurn` JSONL line carrying the AUQ ``tool_use``
        block and the MCP ``_on_tool_call`` invocation arrive on different
        streams (stdout vs Unix socket). In every observed real-claude run
        the AssistantTurn lands first, so the FIFO loop pairs the incoming
        ``request_id`` with the first :attr:`_pending_questions` entry
        whose ``mcp_request_id`` is ``None``.

        If no un-paired entry exists, the request_id is parked in
        :attr:`_unpaired_mcp_auq_calls`. :meth:`_track_ask_user_question`
        consumes from that deque on the next AUQ observation. Walking past
        an already-paired entry is logged as a warning — it means a
        duplicate MCP call landed for an AUQ that already had a pairing
        (claude bug, our pairing bug, or stream replay).
        """
        for pending in self._pending_questions.values():
            if pending.mcp_request_id is None:
                pending.mcp_request_id = request_id
                return
            log.warning(
                "session_manager.auq_mcp_pairing_walk_past_paired",
                tool_use_id=pending.tool_use_id,
                existing_mcp_request_id=pending.mcp_request_id,
                incoming_mcp_request_id=request_id,
            )
        self._unpaired_mcp_auq_calls.append(request_id)

    # ------------------------------------------------------------------ #
    # CCR-026: AskUserQuestion gate.
    # ------------------------------------------------------------------ #

    async def send_tool_result(
        self,
        tool_use_id: str,
        content: str,
        *,
        is_error: bool = False,
    ) -> bool:
        """Feed a synthetic ``tool_result`` block back to the running session.

        Atomically pops :attr:`_pending_questions[tool_use_id]` and cancels
        its timeout task before writing — the pop is the single-shot
        resolution guard against concurrent button taps / ``/answer``
        races.

        CCR-028: when the popped entry carries an ``mcp_request_id`` (the
        AUQ-via-MCP path), the answer is delivered by resolving the MCP
        Future with ``{"behavior": "deny", "message": <content>}``.
        claude's harness writes the synthetic ``tool_result`` itself once
        the deny lands; we MUST NOT also write one via
        :meth:`_deliver_tool_result` or two ``tool_result`` envelopes
        would race for the same ``tool_use_id``. The caller's
        ``is_error`` flag is ignored on the MCP path — claude's harness
        flags every deny as ``is_error=True`` on the wire.

        Legacy path (``mcp_request_id is None``): keeps the existing
        wire write so callers that inject a raw ``tool_result`` outside
        the AUQ-MCP collision window still work.

        Returns ``True`` if THIS call delivered (the id was registered);
        ``False`` if the id was unknown / already resolved / the session
        crashed mid-flight (``RuntimeError`` from
        :meth:`ClaudeProcess.send_user_turn`). The bot maps ``False`` to
        the canned ``"Stale prompt"`` alert — same shape as
        :meth:`resolve_permission`.

        Raises :class:`NoActiveSessionError` when no Claude subprocess is
        held (caller bug — same precondition as :meth:`send`).
        """
        if self._proc is None:
            message = "No active session."
            raise NoActiveSessionError(message)

        pending = self._pending_questions.pop(tool_use_id, None)
        if pending is None:
            return False
        if pending.timeout_task is not None and not pending.timeout_task.done():
            pending.timeout_task.cancel()

        if pending.mcp_request_id is not None:
            decision: dict[str, object] = {"behavior": "deny", "message": content}
            return await self._mcp.resolve(pending.mcp_request_id, decision)

        try:
            await self._deliver_tool_result(tool_use_id, content, is_error=is_error)
        except RuntimeError as exc:
            log.warning(
                "session_manager.send_tool_result_failed",
                tool_use_id=tool_use_id,
                error=str(exc),
            )
            return False
        return True

    def is_question_pending(self, tool_use_id: str) -> bool:
        """Return ``True`` iff a question is registered and unresolved."""
        return tool_use_id in self._pending_questions

    def question_options(self, tool_use_id: str) -> list[str] | None:
        """Return the option list carried by the question, or ``None`` if unknown.

        Used by the bot's ``cb_ask_user_question`` to translate a
        button-index callback into the option text fed back to claude.
        Returns an empty list for free-text questions (which carry no
        button keyboard); the bot's callback handler should never run on
        those because the keyboard is not built.
        """
        pending = self._pending_questions.get(tool_use_id)
        if pending is None:
            return None
        return list(pending.options)

    def outstanding_free_text_questions(self) -> list[str]:
        """Return the ``tool_use_id`` of every outstanding free-text question.

        "Free-text" = registered with an empty ``options`` list. Used by
        the plain-text aiogram filter to decide whether to claim the
        message — the filter requires exactly one entry to fire.
        """
        return [q.tool_use_id for q in self._pending_questions.values() if not q.options]

    def question_id_by_prefix(self, id_prefix: str) -> str | None:
        """Resolve an 8-hex-prefix to the full ``tool_use_id``.

        Returns ``None`` on zero matches OR ≥2 matches (collision). The
        same disambiguation pattern as
        :meth:`_db_lookup_session_by_prefix`. The bot's ``/answer``
        handler maps ``None`` to ``"Stale prompt"``.
        """
        matches = [tid for tid in self._pending_questions if tid.startswith(id_prefix)]
        if len(matches) != 1:
            return None
        return matches[0]

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
        # than a hung MCP socket. ``cancel_pending`` covers AUQ-paired
        # Futures too — they share the same ``_futures`` map.
        await self._mcp.cancel_pending(session_id)
        self._mcp.set_current_session(None)
        # CCR-028: drop any parked-but-unmatched AUQ MCP request_ids.
        # No Future is registered for them under the post-cancel state,
        # so this is plain bookkeeping cleanup.
        self._unpaired_mcp_auq_calls.clear()

        # CCR-026: cancel every outstanding AskUserQuestion timeout task
        # and drop the bookkeeping. We do NOT send synthetic
        # ``tool_result`` blocks back here — the subprocess is being torn
        # down anyway and the unanswered question dies with it.
        for pending in self._pending_questions.values():
            if pending.timeout_task is not None and not pending.timeout_task.done():
                pending.timeout_task.cancel()
        self._pending_questions = {}

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
        self._running_subagents = {}
        self._skills = []
        self._rate_limit_status = None

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
                    self._skills = list(event.skills)
                elif isinstance(event, ResultEvent) and event.subtype == "success":
                    self._saw_result_success = True
                elif isinstance(event, RateLimitEvent):
                    self._rate_limit_status = event

                self._track_subagents(event)
                self._track_ask_user_question(event, session_id)

                self._schedule_last_event_at_update(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "session_manager.consumer_error",
                session_id=str(session_id),
            )

    def _track_subagents(self, event: object) -> None:
        """Update ``_running_subagents`` from a stream-json event.

        Adds an entry on a ``Task``/``Agent`` ``ToolUseBlock`` carrying a
        non-empty ``subagent_type``; removes the matching entry when a
        ``ToolResultBlock`` for the same ``tool_use_id`` arrives. All other
        events are ignored.
        """
        if isinstance(event, AssistantTurn):
            for block in event.message.content:
                if isinstance(block, ToolUseBlock) and block.name in _SUBAGENT_DISPATCH_TOOL_NAMES:
                    subagent_type = str(block.input.get("subagent_type") or "").strip()
                    if subagent_type:
                        self._running_subagents[block.id] = subagent_type
        elif isinstance(event, UserTurn):
            content = event.message.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        self._running_subagents.pop(block.tool_use_id, None)

    def _track_ask_user_question(self, event: object, session_id: uuid.UUID) -> None:
        """Register every ``AskUserQuestion`` ``tool_use`` block in ``event``.

        Walks :attr:`AssistantTurn.message.content` looking for blocks
        with ``name == "AskUserQuestion"`` (kept as a parallel walk to
        :meth:`_track_subagents` so future changes to one do not drag the
        other). For each match, creates a :class:`_PendingQuestion` and
        schedules its timeout task via :meth:`_schedule_question_timeout`.

        Idempotency: if the same ``tool_use_id`` is observed twice
        (impossible in normal operation but cheap to guard), the second
        observation is ignored — the first wins and its timeout task
        keeps running.
        """
        if not isinstance(event, AssistantTurn):
            return
        for block in event.message.content:
            if not isinstance(block, ToolUseBlock):
                continue
            if block.name != _ASK_USER_QUESTION_TOOL_NAME:
                continue
            if block.id in self._pending_questions:
                continue
            options = _extract_ask_user_question_options(block.input)
            # CCR-028: drain a parked AUQ MCP request_id (covers the
            # MCP-arrives-before-AssistantTurn race). In observed
            # real-claude runs this deque is always empty and the
            # pairing happens later in ``_on_mcp_ask_user_question``.
            mcp_request_id = (
                self._unpaired_mcp_auq_calls.popleft() if self._unpaired_mcp_auq_calls else None
            )
            pending = _PendingQuestion(
                tool_use_id=block.id,
                session_id=session_id,
                options=options,
                timeout_task=None,
                mcp_request_id=mcp_request_id,
            )
            self._pending_questions[block.id] = pending
            self._schedule_question_timeout(pending)

    def _schedule_question_timeout(self, pending: _PendingQuestion) -> None:
        """Schedule the per-question timeout fail-safe.

        On fire, the task pops the entry from :attr:`_pending_questions`
        directly and writes an ``is_error=True`` ``tool_result`` via the
        private :meth:`_deliver_tool_result` helper. We do NOT call
        :meth:`send_tool_result` from the timeout because that would
        race with a successful concurrent reply (both would attempt the
        same pop; the loser would no-op silently while the winner has
        already written the answer).
        """
        deadline = float(self._settings.ask_user_question_timeout_seconds)
        pending.timeout_task = asyncio.create_task(
            self._handle_question_timeout(pending.tool_use_id, deadline),
            name=f"auq-timeout-{pending.tool_use_id}",
        )

    async def _handle_question_timeout(self, tool_use_id: str, deadline: float) -> None:
        """Fire-on-deadline handler for an outstanding ``AskUserQuestion``."""
        try:
            await asyncio.sleep(deadline)
        except asyncio.CancelledError:
            return
        pending = self._pending_questions.pop(tool_use_id, None)
        if pending is None:
            return
        log.warning(
            "session_manager.ask_user_question_timeout",
            tool_use_id=tool_use_id,
            timeout_seconds=deadline,
        )
        message = f"Timed out — no paired user responded within {int(deadline)}s"
        if pending.mcp_request_id is not None:
            # CCR-028 — same MCP-deny semantics as the happy-path
            # ``send_tool_result`` branch. claude's harness writes the
            # synthetic ``tool_result`` from the deny decision.
            await self._mcp.resolve(
                pending.mcp_request_id,
                {"behavior": "deny", "message": message},
            )
            return
        try:
            await self._deliver_tool_result(tool_use_id, message, is_error=True)
        except (RuntimeError, NoActiveSessionError) as exc:
            log.warning(
                "session_manager.ask_user_question_timeout_send_failed",
                tool_use_id=tool_use_id,
                error=str(exc),
            )

    async def _deliver_tool_result(
        self,
        tool_use_id: str,
        content: str,
        *,
        is_error: bool,
    ) -> None:
        """Write a synthetic ``tool_result`` user-turn to the live subprocess.

        Does NOT touch :attr:`_pending_questions` — callers must pop the
        entry themselves before calling this. Both
        :meth:`send_tool_result` and the timeout task pop first; this
        helper only handles the wire-level write.
        """
        if self._proc is None:
            message = "No active session."
            raise NoActiveSessionError(message)
        block = ToolResultBlock(
            type="tool_result",
            tool_use_id=tool_use_id,
            content=content,
            is_error=is_error,
        )
        await self._proc.send_user_turn([block])

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

    def running_subagents(self) -> list[str]:
        """Return a snapshot of subagent types currently running.

        A subagent is "running" iff a ``tool_use`` block with name in
        :data:`_SUBAGENT_DISPATCH_TOOL_NAMES` has been observed in this
        session and no ``tool_result`` for that ``tool_use_id`` has been
        observed yet. Returns an alphabetically sorted, deduplicated list.
        Returns ``[]`` when no session is running OR no subagents have
        been dispatched.
        """
        return sorted(set(self._running_subagents.values()))

    def available_skills(self) -> list[str]:
        """Return a sorted snapshot of skills from the most recent system/init event.

        Returns ``[]`` when no session is running or the init event carried no skills.
        """
        return sorted(self._skills)

    def is_session_active(self) -> bool:
        """Return ``True`` iff a Claude subprocess is currently held."""
        return self._proc is not None

    def current_session_usage(self) -> SessionUsage | None:
        """Return aggregated usage for the live session, or ``None`` if idle.

        Walks the running session's JSONL log via
        :func:`ccr.claude.usage.aggregate_session_usage`. Read-only — does
        not block the consumer task. Returns ``None`` when no Claude
        subprocess is currently held; the bot's ``/cost`` handler maps
        that to the canonical ``"No active session."`` reply.
        """
        session_id = self._session_id
        if session_id is None or self._proc is None:
            return None
        return aggregate_session_usage(self._logs_dir / f"{session_id}.jsonl")

    def current_rate_limit_status(self) -> RateLimitEvent | None:
        """Return the most recent rate-limit snapshot for the live session.

        Returns ``None`` if no session is running OR no ``rate_limit_event``
        has been observed yet on this session. Last-one-wins: a later
        ``rate_limit_event`` replaces the snapshot. The bot's ``/usage``
        handler renders this snapshot directly — claude's own ``-p`` reply
        to ``/usage`` is a useless one-liner (probe CCR-032), so the local
        render is strictly better than forwarding.
        """
        if self._proc is None:
            return None
        return self._rate_limit_status


__all__ = [
    "NoActiveSessionError",
    "NoPriorSessionError",
    "SessionAlreadyRunningError",
    "SessionError",
    "SessionManager",
    "SessionNotFoundError",
    "StaleSessionError",
    "StaleToolUseError",
]
