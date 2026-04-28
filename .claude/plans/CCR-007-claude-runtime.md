# Plan: CCR-007 — Claude subprocess wrapper, event bus, JSONL logger

## Goal

Stand up the `claude-runtime` feature: two brand-new packages (`src/ccr/events/` and `src/ccr/claude/`) that together let a single global `SessionManager` spawn `claude -p --input-format=stream-json --output-format=stream-json --verbose`, parse its stdout into typed `ClaudeEvent` objects, persist them to `data/logs/<session_id>.jsonl`, publish them on an in-process `EventBus`, and update the corresponding `Session` row through a `running → completed | stopped | crashed` state machine. After this ticket lands, every later phase has a stable surface to consume: the bot broadcast task subscribes to the bus (CCR-008), the SSE endpoint replays JSONL then tails the bus (CCR-012), and the permission handler (CCR-009) and slash passthrough (CCR-010) extend `SessionManager`.

This is the first and only ticket in `claude-runtime`; `BRIEF.md` and `CONTEXT.md` are stubs. The architect must establish the full module boundary with care because four downstream features are gated on these abstractions.

## File layout

New package `src/ccr/events/` — pub/sub primitive shared across the project.

- `src/ccr/events/__init__.py` — create — re-exports `EventBus`, `Subscriber`. Public surface for downstream importers (`from ccr.events import EventBus`).
- `src/ccr/events/bus.py` — create — `class EventBus` with weakref-tracked subscribers, bounded queues, drop-oldest backpressure, structured-log warning on drop.

New package `src/ccr/claude/` — process wrapper, event schema, log, manager.

- `src/ccr/claude/__init__.py` — create — re-exports `SessionManager`, `SessionStatus`, `ClaudeEvent`, `ContentBlock`, `JsonlSessionLog`. Stable import path consumed by `ccr.bot.formatting` (CCR-008), `ccr.web.sessions` (CCR-012), and the slash passthrough handler (CCR-010).
- `src/ccr/claude/events.py` — create — Pydantic v2 discriminated-union `ClaudeEvent` and `ContentBlock` with all six outer variants and four inner blocks. Includes the synthetic `error` event variant published on subprocess crash.
- `src/ccr/claude/process.py` — create — `class ClaudeProcess` owning the subprocess; opens stdin/stdout/stderr, parses lines, surfaces stderr only on crash.
- `src/ccr/claude/log.py` — create — `class JsonlSessionLog` (per-session append-only file, `read_from(seq)`, `tail()` with shared `asyncio.Event`) and module-level `prune(data_dir, retention_count, retention_days)`.
- `src/ccr/claude/manager.py` — create — `class SessionManager` enforcing the global single-session invariant, owning the bus + log + DB write side, and emitting `session.event` and `session.status` topics.
- `src/ccr/claude/state.py` — create — `class SessionStatus(StrEnum)` with `idle`, `running`, `completed`, `stopped`, `crashed`. (Five values; the `Session.status` SQL column already accepts `running | completed | stopped | crashed`. `idle` is in-memory only — `SessionManager.status()` returns it when no session row is active. We keep the SQL surface unchanged.)
- `tests/fakes/__init__.py` — create — empty marker so `tests/fakes/` is importable.
- `tests/fakes/fake_claude.py` — create — small Python script reading JSONL from stdin, emitting canned JSONL to stdout based on env-var directives. Driven via `python -m tests.fakes.fake_claude` from `SessionManager` tests with `CLAUDE_BIN` overridden.
- `tests/test_event_bus.py` — create — pub/sub semantics, slow-consumer drop, weakref cleanup on subscriber GC, fan-out to multiple subscribers, cancel safety of `subscribe()`. (Plan lists `test_session_manager.py` only; this file is added by the architect because the bus is a load-bearing primitive consumed by three later phases — testing it through `SessionManager` alone would mask subscriber-lifecycle bugs.)
- `tests/test_claude_events.py` — create — round-trip parses each known event variant from fixture JSONL; `UnknownEvent` for unrecognized `type`.
- `tests/test_claude_log.py` — create — append → `read_from` returns `(seq, event)` pairs starting at the right index; `tail()` resumes after `read_from`; concurrent `tail()` callers all see new events; `prune()` retention rules.
- `tests/test_session_manager.py` — create — uses `tests/fakes/fake_claude.py` via `CLAUDE_BIN` override; covers full lifecycle including crash detection.

## Public surface

### `src/ccr/events/bus.py`

```python
# Sketch — illustrative, not the final code.
from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_DEFAULT_QUEUE_MAXSIZE = 256


class Subscriber:
    """Owns one bounded queue. Held by weakref in EventBus._subs.

    The async generator returned by EventBus.subscribe() holds a *strong*
    reference to its Subscriber for the duration of the iteration; when
    the consumer breaks / cancels / GCs the generator, the Subscriber is
    released and the EventBus's WeakSet entry drops on its next publish().
    """

    def __init__(self, topic: str, maxsize: int) -> None:
        self.topic = topic
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)


class EventBus:
    def __init__(self, *, queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE) -> None:
        # WeakSet so an iterator that's GC'd without aclose() still drops out.
        self._subs: dict[str, weakref.WeakSet[Subscriber]] = {}
        self._queue_maxsize = queue_maxsize

    async def publish(self, topic: str, payload: Any) -> None:
        for sub in list(self._subs.get(topic, ())):
            try:
                sub.queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop oldest to stay bounded (back-pressure policy).
                try:
                    sub.queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover — race
                    pass
                sub.queue.put_nowait(payload)
                log.warning(
                    "event_bus.slow_consumer_drop",
                    topic=topic,
                    qsize=sub.queue.qsize(),
                )

    async def subscribe(self, topic: str) -> AsyncIterator[Any]:
        sub = Subscriber(topic=topic, maxsize=self._queue_maxsize)
        self._subs.setdefault(topic, weakref.WeakSet()).add(sub)
        try:
            while True:
                yield await sub.queue.get()
        finally:
            # WeakSet drops automatically when `sub` is collected, but be
            # explicit so cancellation cleans up immediately.
            self._subs.get(topic, weakref.WeakSet()).discard(sub)
```

Public exports:

- `EventBus()` — no constructor args required; `queue_maxsize` keyword-only knob.
- `EventBus.publish(topic: str, payload: Any) -> None` — non-blocking, drops oldest on slow consumer with structured-log warning at `WARNING`.
- `EventBus.subscribe(topic: str) -> AsyncIterator[Any]` — async generator. Caller iterates with `async for payload in bus.subscribe(topic): ...`. Cancellation / `break` / `aclose()` releases the queue. Multiple concurrent subscribers per topic are supported (fan-out). No bounded subscriber count.

Topics this ticket establishes (per plan §6.4):

- `"session.event"` — payload `{"session_id": UUID, "event": ClaudeEvent}`. Emitted from `SessionManager._consume_events()` for every parsed event including the synthetic crash error.
- `"session.status"` — payload `{"session_id": UUID, "status": SessionStatus, "ts": datetime}`. Emitted on every state transition.

### `src/ccr/claude/events.py`

The plan only sketches the outer union shape (§6.5). The architect specifies the concrete field set below, derived from the Claude Code `--output-format=stream-json` payloads referenced in the plan. Phase 8 (permission gating), Phase 9 (passthrough), and Phase 11 (SSE replay) pattern-match on these fields, so the schema is fixed here.

```python
# Sketch — illustrative, not the final code.
from __future__ import annotations

from typing import Annotated, Any, Literal, Union
from pydantic import BaseModel, ConfigDict, Field


# --- inner content blocks ---------------------------------------------------

class TextBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["text"]
    text: str


class ThinkingBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["thinking"]
    thinking: str


class ToolUseBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["tool_result"]
    tool_use_id: str
    content: str | list[dict[str, Any]] | None = None
    is_error: bool = False


ContentBlock = Annotated[
    Union[TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock],
    Field(discriminator="type"),
]


# --- outer events -----------------------------------------------------------

class _EventBase(BaseModel):
    model_config = ConfigDict(extra="allow")  # forward-compatible


class SystemInit(_EventBase):
    type: Literal["system"]
    subtype: Literal["init"]
    session_id: str | None = None
    model: str | None = None
    tools: list[str] | None = None


class _UserMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Literal["user"]
    content: str | list[ContentBlock]


class UserTurn(_EventBase):
    type: Literal["user"]
    message: _UserMessage


class _AssistantMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Literal["assistant"]
    content: list[ContentBlock]


class AssistantTurn(_EventBase):
    type: Literal["assistant"]
    message: _AssistantMessage


class ResultEvent(_EventBase):
    type: Literal["result"]
    subtype: Literal["success", "error_during_execution"]
    duration_ms: int | None = None
    total_cost_usd: float | None = None


class PermissionRequest(_EventBase):
    type: Literal["permission_request"]
    request_id: str
    tool_use_id: str | None = None
    tool_name: str
    input: dict[str, Any] = Field(default_factory=dict)
    options: list[str]


class UnknownEvent(_EventBase):
    """Catch-all for unrecognized type values OR validation failures."""
    type: str  # NOT a Literal — accepts anything not matched above
    raw: dict[str, Any]


ClaudeEvent = Annotated[
    Union[
        SystemInit,
        UserTurn,
        AssistantTurn,
        ResultEvent,
        PermissionRequest,
        UnknownEvent,
    ],
    Field(discriminator="type"),
]
```

Notes that constrain the developer:

- `extra="allow"` on every model — Claude Code's stream-json schema may add fields; we keep them in `model_extra` rather than dropping or raising.
- Discriminator on `type`. `UnknownEvent.type: str` is intentional: when no `Literal` matches, Pydantic's `union_mode="smart"` + the catch-all `str` type means `UnknownEvent` wins. The developer's parse helper must wrap `TypeAdapter(ClaudeEvent).validate_python(obj)` in a `try/except ValidationError` and on failure produce `UnknownEvent(type=obj.get("type", "unknown"), raw=obj)`. Belt-and-suspenders: schema shifts that make a *known* variant fail validation should still surface as `UnknownEvent`, not crash the consumer.
- The synthetic crash event uses `UnknownEvent(type="error", raw={"reason": ..., "exit_code": ..., "stderr_tail": ...})`. The plan calls it "synthetic error event"; encoding it as `UnknownEvent` keeps the union closed and avoids inventing a new schema variant for a runtime-only signal. Stderr is buffered and surfaced *only on crash* per plan task 5.
- Stderr buffer size cap: `STDERR_TAIL_BYTES = 8192` — last N bytes preserved; older content discarded. Prevents unbounded memory on chatty subprocess.

Helper:

```python
# Sketch — illustrative, not the final code.
from pydantic import TypeAdapter, ValidationError

_ADAPTER = TypeAdapter(ClaudeEvent)


def parse_event(line: str | bytes | dict[str, Any]) -> ClaudeEvent:
    """Parse a single stream-json line into a ClaudeEvent.

    Falls back to UnknownEvent on JSON-decode failure, schema mismatch,
    or unknown `type`.
    """
```

### `src/ccr/claude/process.py`

```python
# Sketch — illustrative, not the final code.
class ClaudeProcess:
    def __init__(
        self,
        *,
        settings: Settings,
        cwd: Path | None = None,
    ) -> None: ...

    async def start(self) -> None:
        """Spawn `claude -p --input-format=stream-json --output-format=stream-json --verbose`.

        Idempotent guard: raises RuntimeError if already started. After start,
        `events()` is the single permitted reader of stdout.
        """

    async def send_user_turn(self, content: str | list[ContentBlock]) -> None:
        """Encode + write a `{"type":"user","message":{...}}` line and drain."""

    async def send_permission_response(self, request_id: str, choice: str) -> None:
        """Encode + write a `{"type":"permission_response", ...}` line and drain.

        Exact wire format keyed to Claude Code's stream-json input schema —
        document the reference in the docstring.
        """

    async def events(self) -> AsyncIterator[ClaudeEvent]:
        """Yield parsed events from stdout until EOF.

        - On JSON decode error or schema mismatch: yield UnknownEvent and continue.
        - On EOF: stop iteration normally.
        - Stderr is read by a sibling task into a bounded ring buffer.
        """

    async def stop(self, grace: float | None = None) -> int:
        """Idempotent. Sends SIGTERM, waits up to `grace` (default settings.subprocess_grace_kill_seconds), then SIGKILL.
        Returns final exit code, or -1 if already stopped.
        """

    @property
    def stderr_tail(self) -> bytes:
        """Last STDERR_TAIL_BYTES bytes of the subprocess's stderr."""
```

### `src/ccr/claude/log.py`

```python
# Sketch — illustrative, not the final code.
class JsonlSessionLog:
    """Per-session append-only JSONL. Sequence numbers = line index (0-based)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()       # serializes append
        self._notify = asyncio.Event()    # set by append, wakes tailers
        self._seq = 0                     # advanced under _lock

    async def open(self) -> None:
        """Compute initial _seq from existing file size (line count). Idempotent."""

    async def append(self, event: ClaudeEvent) -> int:
        """Append one JSONL line. Returns the assigned seq.
        Holds _lock for the duration of the write+flush; sets _notify after."""

    async def read_from(self, seq: int) -> AsyncIterator[tuple[int, ClaudeEvent]]:
        """Read existing lines starting at `seq`. Stops at current EOF.
        Cancel-safe: closes the file on cancellation."""

    async def tail(self) -> AsyncIterator[tuple[int, ClaudeEvent]]:
        """Yield events appended after the call begins.

        Implementation: snapshot _seq, then loop:
          1. wait on _notify.
          2. immediately clear _notify (the *next* append will re-set it).
          3. read every line whose index >= snapshot_seq, advance snapshot.

        Multiple concurrent tail() callers are supported — `_notify` is shared.
        Cancel-safe: file handle is reopened per drain pass and closed before
        re-waiting, so cancellation never leaks an FD.
        """


def prune(
    logs_dir: Path,
    *,
    retention_count: int,
    retention_days: int,
    now: datetime | None = None,
) -> int:
    """Startup-time cleanup. Keeps last `retention_count` files by mtime;
    deletes any file with mtime older than `retention_days`. Returns deleted count.
    """
```

The `_notify` design must satisfy three properties (acceptance and tests):

1. **No missed wakeup.** A consumer that calls `tail()` and then misses a `notify_set` because `append()` ran first must still see the new event on its first `read_from` pass. Solved by having `tail()` snapshot `_seq` *before* the first `wait()`; if the file already grew between snapshot and wait, the first drain pass returns those events and the wait is a no-op (because `notify` is set).
2. **Multiple concurrent tailers.** `asyncio.Event.set()` wakes all waiters; each clears its own snapshot of progress. Clearing `_notify` is safe because each tailer drains under its own snapshot, not the shared event.
3. **Cancel safety.** Reopen the file at the top of each drain pass; close before `await self._notify.wait()`. No file handle held across `await`-points that could be cancelled.

### `src/ccr/claude/state.py`

```python
# Sketch — illustrative, not the final code.
from enum import StrEnum

class SessionStatus(StrEnum):
    IDLE = "idle"            # in-memory only; never written to Session.status
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"
    CRASHED = "crashed"
```

### `src/ccr/claude/manager.py`

```python
# Sketch — illustrative, not the final code.
class SessionManager:
    """Globally enforces a single running Claude subprocess.

    Composition:
      - bus: EventBus  (injected — same instance shared with bot/web)
      - db_factory: async_sessionmaker[AsyncSession]
      - settings: Settings
      - logs_dir: Path  (settings.data_dir / "logs")

    Internal state guarded by a single asyncio.Lock (the "session lock"):
      - _proc: ClaudeProcess | None
      - _session_id: UUID | None
      - _log: JsonlSessionLog | None
      - _consumer_task: asyncio.Task | None     (drains _proc.events())
      - _exit_task: asyncio.Task | None         (awaits _proc subprocess exit)
      - _status: SessionStatus                   (in-memory)
    """

    async def new_session(
        self,
        *,
        prompt: str | None,
        started_by_tg_user_id: int | None,
    ) -> UUID:
        """Stop the current session if any, spawn a fresh one, optionally
        send `prompt` as the first user turn. Returns the new Session.id.

        Concurrency rule: take the session lock for the entire transition.
        Two concurrent callers serialize; the second sees the first's session
        as "current" and stops it before spawning its own."""

    async def send(self, prompt: str) -> None:
        """Forward a user turn to the running session.
        Raises NoActiveSessionError if _status != RUNNING."""

    async def send_permission(
        self, session_id: UUID, request_id: str, choice: str
    ) -> None:
        """Forward a permission response. Validates session_id matches the
        current running session; raises StaleSessionError otherwise."""

    async def stop(self) -> None:
        """Idempotent: no-op if no running session.
        Order: SIGTERM → wait grace → SIGKILL → wait exit task → set status
        STOPPED → update Session row → publish session.status."""

    async def status(self) -> SessionStatus:
        """In-memory status. IDLE if _proc is None."""

    # --- internals ---

    async def _consume_events(self) -> None:
        """Background task: for each event from _proc.events():
            seq = await _log.append(event)
            await _bus.publish("session.event", {"session_id": ..., "event": event})
            update Session.last_event_at
        On normal exit (EOF): rely on _exit_task to set final status."""

    async def _await_exit(self) -> None:
        """Background task: await self._proc._proc.wait(); decide final status:
            exit_code == 0 AND saw a `result.success` event → COMPLETED
            exit_code == 0 AND saw `result.error_during_execution`     → CRASHED
            stop() was invoked → STOPPED  (cooperate via a flag)
            else                                          → CRASHED + emit
                                                            UnknownEvent(type="error", ...)
        Update Session row, publish session.status, drain _consumer_task."""
```

State machine (in-memory `_status`):

```
                  new_session()
       ┌─────────────────────────┐
       │                         ▼
   IDLE ─── new_session() ───► RUNNING ─── proc EOF + result.success ──► COMPLETED
                                  │
                                  ├─ stop() ──────────────────────────► STOPPED
                                  │
                                  └─ proc EOF (other) | exit_code != 0 ► CRASHED
                                                                          │
                                                                          ▼
                                                       publish UnknownEvent(type="error", ...)
```

`new_session()` from any non-IDLE state implicitly transitions to STOPPED first (calling `self.stop()` internally) before the new session starts. Per plan: "Stop running session if any, spawn fresh Claude Code subprocess." — the choice is **silent stop**, not raise. This matches the `/new` UX in CCR-008.

Errors the developer must define and export:

```python
class SessionError(Exception): ...
class NoActiveSessionError(SessionError): ...
class StaleSessionError(SessionError): ...
```

## Patterns and prior art

- **Reuse `db.engine.AsyncSessionMaker`** (`src/ccr/db/engine.py:36`) — `SessionManager.__init__` takes `db_factory: async_sessionmaker[AsyncSession]`, mirroring how `ccr.bot.app.build_dispatcher` (`src/ccr/bot/app.py:32`) and `ccr.server.serve` (`src/ccr/server.py:24`) thread the factory. Open a short-lived session per write (`new_session`, status updates, `last_event_at`) and commit eagerly. Do not hold a session across the whole subprocess lifetime.
- **Reuse the lazy-typing pattern** from `src/ccr/auth/pairing.py:22` — `if TYPE_CHECKING:` for `AsyncSession`, `async_sessionmaker`, `Settings` imports keeps the module light at import time and matches the project style.
- **Reuse `structlog`** for structured logs (see every existing module). One logger per module: `log = structlog.get_logger(__name__)`. Slow-consumer drops, subprocess crashes, schema fall-throughs are all WARN-level events with structured fields.
- **Reuse the `__init__.py` re-export pattern** from `src/ccr/auth/__init__.py:1` and `src/ccr/db/__init__.py:1` — explicit `__all__`, no wildcard imports, errors and types both re-exported.
- **Reuse Pydantic v2 discriminated-union convention** (config already lives in pydantic-settings — same world). The plan and CLAUDE.md call out `Annotated[Union[...], Field(discriminator="type")]` as the canonical shape; follow it.
- **Avoid:** a generic `Event` base class with subclassing for variants — the plan locks in the discriminated-union approach, and pattern-matching on `event.type` is what every consumer uses. Don't introduce inheritance-based polymorphism here.
- **Avoid:** writing the JSONL through SQLAlchemy or any async I/O wrapper — it's a plain file append protected by an `asyncio.Lock`. Plan §6.4 and CLAUDE.md both anchor this: "JSONL events… append-only, with sequence numbers derived from line count."
- **Avoid:** holding a DB session for the lifetime of a subprocess. Open per-write. The `Session` row update on `last_event_at` should batch-coalesce (e.g., update at most once per second) to avoid hammering SQLite — implement coalescing as a simple "last write wins, fire-and-forget after debounce" in `_consume_events`.

## Abstractions

Three new abstractions land in this ticket. Each is justified by ≥ 3 concrete callers landing in the next four phases:

1. **`EventBus`** — consumed by (a) the Telegram broadcast task in `server.py` (CCR-008), (b) the SSE endpoint in `web/sessions.py` (CCR-012), (c) the future doctor / health endpoint, and (d) `SessionManager` itself for `session.status`. Four callers → abstraction is justified now.
2. **`ClaudeEvent` discriminated union** — consumed by (a) `bot.formatting.event_to_messages` (CCR-008), (b) the SSE endpoint serializer (CCR-012), (c) the viewer JS dispatcher (CCR-013, indirectly via JSON), (d) the permission handler (CCR-009). Four callers.
3. **`SessionManager`** — consumed by (a) bot session handlers (`/new`, `/stop`, `/clear`, plain text — CCR-008), (b) the permission handler (CCR-009), (c) the slash passthrough (CCR-010), (d) `web/sessions.py` `/healthz` (CCR-012). Four callers.

`SessionStatus` and `JsonlSessionLog` are not standalone abstractions — they are internal implementation details of `SessionManager` exposed because the SSE replay path needs `JsonlSessionLog.read_from` directly. That direct exposure is a *deliberate* leak, not a new abstraction.

No new abstraction for "writing to SQLite per session-state change" — that is two `db_factory()` blocks in `SessionManager`. Three lines is better than a premature abstraction.

## Dependencies

**Depends on:**
- `src/ccr/config.py` settings keys: `claude_bin`, `claude_extra_args`, `data_dir`, `log_retention_count`, `log_retention_days`, `subprocess_grace_kill_seconds`. All exist (verified in `src/ccr/config.py:55-62`).
- `src/ccr/db/models.py::Session` — written by `SessionManager` on lifecycle transitions (`status`, `started_at`, `ended_at`, `started_by_tg_user_id`, `first_prompt`, `exit_reason`, `last_event_at`). All columns exist (verified in `src/ccr/db/models.py:91-118`).
- `src/ccr/db/engine.py::AsyncSessionMaker` — for short-lived DB writes.
- Standard library: `asyncio`, `json`, `weakref`, `pathlib`, `enum`. Pydantic v2 (already installed).
- No new external packages.

**Used by (future tickets):**
- CCR-008 (chat-bot session handlers): subscribes to `session.event` for the broadcast task; calls `new_session`, `send`, `stop`, `status`.
- CCR-009 (permission inline buttons): adds gating fields onto `SessionManager`; calls `send_permission`.
- CCR-010 (slash passthrough): adds `send_slash` onto `SessionManager`.
- CCR-012 (web SSE): subscribes to `session.event` filtered by `session_id`; reads `JsonlSessionLog.read_from(seq)` for replay.
- CCR-016 (doctor): calls `SessionManager.status()` and checks `claude_bin` reachability (the latter uses `settings.claude_bin`, not `SessionManager` directly).

## Edge cases the developer must handle

- **Subprocess never starts.** `claude_bin` not on PATH → `FileNotFoundError` from `create_subprocess_exec`. Catch in `ClaudeProcess.start`, raise a clean `SessionError("claude binary not found: <path>")`. `SessionManager.new_session` propagates; the caller's `Session` row must NOT be inserted before subprocess start succeeds — write the row only after `start()` returns cleanly.
- **Subprocess exits before any event.** `_consume_events` sees EOF immediately. `_await_exit` decides: exit code 0 with no events seen → `CRASHED` (we expected at least a `system.init`). Synthetic error event published.
- **`stop()` called while no session.** Idempotent no-op, returns cleanly. Logged at DEBUG.
- **`stop()` called twice in succession.** Second call sees `_proc is None` (already torn down) → no-op.
- **`new_session()` while another is running.** Internally calls `self.stop()` (silent transition to STOPPED), then proceeds. Single session lock serializes; the second concurrent `new_session()` sees the first's transition and behaves correctly.
- **Subprocess writes a malformed JSON line.** `parse_event` returns `UnknownEvent(type="parse_error", raw={"line": <truncated>})`. Logged at WARN. Stream continues.
- **Subprocess writes an unknown `type`.** `UnknownEvent(type=<that_string>, raw=<full_obj>)` — preserves the raw payload for the viewer. Logged at INFO.
- **Subprocess emits a partial JSON line at EOF (no trailing newline).** Parser must accumulate into a buffer and only call `parse_event` on `\n`-terminated chunks. The trailing partial chunk is logged at WARN and discarded.
- **Stderr exceeds 8 KiB.** Ring-buffer keeps last `STDERR_TAIL_BYTES` bytes only.
- **Two paired users `/new` simultaneously.** Session lock serializes; both succeed in order, the second's session supersedes the first. Plan invariant: "started_by_tg_user_id records who kicked it off but any paired user can interact" — recorded faithfully.
- **`_log.append()` and `_bus.publish()` ordering.** Append to log *before* publishing — replay-then-tail (CCR-012's SSE) relies on every published event being already on disk so a late subscriber doesn't miss it.
- **`Session.last_event_at` write rate.** Coalesce with a 1-second debounce. Don't write per event for chatty assistant turns.
- **DB write failure during status transition.** Log error, continue — the in-memory `_status` is the source of truth for the current process. The `Session` row may be temporarily stale; SSE clients see the bus-published status either way.
- **Crash during `stop()` grace period.** If subprocess dies (returncode set) before SIGKILL is needed, `stop()` proceeds normally; final status is STOPPED (intentional stop overrides the spontaneous exit).
- **`prune()` startup race with active session.** Prune runs before the first `new_session()` (call it from `SessionManager.start()` or from `serve()` startup). Document that prune must NOT run concurrently with appending to a log file.
- **EventBus subscriber GC mid-publish.** WeakSet drops the entry; `publish()` iterates a snapshot list (`list(self._subs.get(topic, ()))`) so a concurrent GC during iteration is safe.
- **EventBus slow consumer wedged forever.** Drop-oldest policy with one WARN log per drop is sufficient; no consumer eviction. Accepts the documented trade-off (the SSE consumer briefly behind sees gaps; old data is in JSONL anyway).
- **Synthetic error event sequence number.** Append it to JSONL too — the SSE replay must include it so a late client reconstructs the crash.
- **`Session.status` enum mismatch.** SQL accepts `running | completed | stopped | crashed` (per CCR-003). Never write `IDLE` to the row — `IDLE` is in-memory only. Validate at the write boundary.
- **`new_session(prompt=...)` ordering.** Insert Session row → start subprocess → wait for `system.init` event → THEN send the user turn. Otherwise the user turn races the init and Claude Code rejects it. Document this ordering in `_run_session`'s docstring; tests must cover it.

## Test surface

`tests/test_event_bus.py`:
- `test_publish_to_no_subscribers_is_noop` — golden path, no exception.
- `test_single_subscriber_receives_published_payloads` — fan-in for one subscriber.
- `test_multiple_subscribers_each_receive_every_payload` — fan-out.
- `test_slow_consumer_drops_oldest_with_warning` — full queue, capture WARN log via `caplog` / structlog testing helper.
- `test_subscriber_iteration_cleanup_on_break` — `async for` with `break` releases the queue; a follow-up `publish()` does not raise.
- `test_subscriber_garbage_collected_iterator_drops_from_set` — explicit `del`, `gc.collect()`, then `publish()`; assert no warning, no error.
- `test_two_topics_isolated` — payload on topic A does not show up for topic-B subscribers.
- `test_subscribe_is_cancel_safe` — cancel the subscriber task mid-iteration; subsequent `publish()` does not block or raise.

`tests/test_claude_events.py`:
- `test_round_trip_system_init` — fixture JSON line → `SystemInit`; key fields preserved.
- `test_round_trip_user_turn_string_content` and `..._block_content` — both content shapes accepted.
- `test_round_trip_assistant_turn_with_text_thinking_tool_use` — discriminated inner blocks.
- `test_round_trip_result_success_and_error` — both subtypes.
- `test_round_trip_permission_request_with_options` — options preserved.
- `test_unknown_type_falls_through_to_unknown_event` — `{"type":"weather_report"}` → `UnknownEvent(type="weather_report", raw=...)`.
- `test_malformed_json_returns_unknown_event_parse_error` — `parse_event("not json")` → `UnknownEvent(type="parse_error", ...)`.
- `test_extra_fields_preserved_in_model_extra` — forward-compat.
- `test_json_schema_is_non_empty` — exact wording from acceptance bullet 4: `TypeAdapter(ClaudeEvent).json_schema()` returns a non-empty dict.

`tests/test_claude_log.py`:
- `test_append_assigns_sequential_seqs_starting_at_zero` — first append → seq 0, second → 1.
- `test_open_resumes_seq_from_existing_file` — pre-write 3 lines, `open()`, append → seq 3.
- `test_read_from_yields_existing_lines_in_order` — pre-seed, iterate, assert.
- `test_read_from_at_or_past_eof_yields_nothing` — boundary.
- `test_tail_yields_appends_after_subscribe` — start `tail()`, append, assert receipt.
- `test_two_concurrent_tailers_both_see_appends` — concurrent `tail()` callers both receive every event.
- `test_tail_is_cancel_safe` — cancel the tail task; appending again does not raise.
- `test_prune_keeps_last_n_files` — fixture dir with 5 files, retention_count=3, assert 2 deleted (oldest).
- `test_prune_deletes_files_older_than_retention_days` — mtime-rewind fixture, assert age-based deletion.

`tests/fakes/fake_claude.py`:
- Reads JSONL from stdin (so `send_user_turn` writes are observed).
- Env directives: `FAKE_CLAUDE_SCRIPT=path/to/canned.jsonl` (lines to emit), `FAKE_CLAUDE_DELAY_MS=N` (per-line delay), `FAKE_CLAUDE_EXIT_CODE=N` (final exit code), `FAKE_CLAUDE_ABORT_AFTER=N` (kill itself after N lines to simulate a crash).
- Emits canned JSONL to stdout, optionally to stderr if `FAKE_CLAUDE_STDERR=...`.
- Used via `CLAUDE_BIN=/path/to/python` plus extra args, OR more cleanly: drop a tiny shim shell script in `tests/fakes/fake_claude` that `exec python -m tests.fakes.fake_claude "$@"` — preferred, decouples settings from test layout.

`tests/test_session_manager.py` (the load-bearing acceptance test):
- `test_lifecycle_idle_to_running_to_completed` — exactly the acceptance bullet: 5 events from fake → 5 lines in JSONL with seq 0–4 → bus subscriber receives all 5 → status transitions.
- `test_subprocess_kill_marks_session_crashed_and_emits_error_event` — acceptance bullet: kill mid-stream, assert `Session.status='crashed'` in DB, assert subscriber received `UnknownEvent(type="error", ...)`.
- `test_stop_is_idempotent` — `stop()` twice, no exception.
- `test_new_session_while_running_stops_old_first` — first `new_session` returns id A, second returns id B; assert DB row for A has `status='stopped'`, B has `status='running'` then `'completed'` after fake exits.
- `test_send_without_active_session_raises` — `NoActiveSessionError`.
- `test_send_permission_with_stale_session_id_raises` — `StaleSessionError`.
- `test_first_prompt_recorded_truncated_to_500` — verify `Session.first_prompt` truncation.
- `test_last_event_at_is_updated_with_debounce` — assert at least one update happens, debounce keeps it ≤ N writes.

Coverage target: the project gate is 80%. The above suite should comfortably clear that on the new modules.

## Out of scope

Mirroring the ticket's "Out of scope" plus architect additions:

- Real Claude Code invocation in tests — fake subprocess only.
- Bot integration (CCR-008) — `event_to_messages`, broadcast task, `/new` / `/stop` / `/clear` handlers.
- Web integration (CCR-012) — SSE endpoint, `/api/sessions`, `/healthz`. The `JsonlSessionLog.read_from` API is exposed *for* CCR-012 but not consumed here.
- Permission gating logic (CCR-009) — `pending_permissions` dict, `is_telegram_paused`, broadcast pause/buffer. The architect resists the temptation to add a no-op `is_telegram_paused` here; it lands clean in CCR-009.
- Slash passthrough (CCR-010) — `send_slash` is added in CCR-010, not here.
- `prune()` invocation site — the function is implemented and unit-tested here, but the *caller* lives in `serve()` (CCR-016 / CCR-017). For CCR-007, `SessionManager.start()` may call it as a one-shot, but do not edit `src/ccr/server.py`.
- `serve()` orchestration changes — Phase 11 (CCR-012) wires the bus + manager into `server.serve()`. Don't touch `server.py` here. `SessionManager` is constructible and unit-testable without `serve()`.
- Doctor / preflight (CCR-016).
- Real wire format of `permission_response` JSON — Claude Code's input schema for permission responses is referenced in the plan but not specified verbatim. Pick the format that matches Claude Code's documentation at implementation time; document the chosen shape in `send_permission_response`'s docstring. If the format is genuinely undefined upstream, raise to team-lead before merging — but do **not** block this ticket on it; a best-effort encoding plus a TODO comment is acceptable since CCR-009 is the consumer that would surface a wire-format mismatch in practice.

## Open questions for team lead

None that block design. Two items the architect flags for the team lead's awareness:

1. **`tests/fakes/fake_claude` shim file.** The ticket's `Files:` list mentions `tests/fakes/fake_claude.py` only. The architect recommends adding a tiny `tests/fakes/fake_claude` (no `.py`) executable shim so `CLAUDE_BIN` can point at it directly without test code synthesizing the path. This is purely test-side; not a scope creep, just a friendlier handoff. Documented under "Test surface" — the developer may inline this differently if they prefer.
2. **`permission_response` wire format.** Recorded under "Out of scope" with the recommendation to ship a best-effort encoding + a docstring TODO. Team lead may want to dispatch a one-line research task before CCR-009 (not CCR-007) to nail the format down upstream.
