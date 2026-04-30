# Plan: CCR-020 — `/continue` command

## Goal

Give Telegram users a `/continue` command that resumes the most recent finished Claude session, sharing its conversation context. The command stops nothing (it errors if a session is running), looks up the most recent `Session` row with status `completed` or `stopped`, spawns a fresh `ClaudeProcess` with the appropriate Claude-side resume flag, mints a new local `Session.id`, and replies `f"Session {id8} resumed (pid {pid})."` — matching the `(pid N)` style introduced by CCR-018.

The architect designs **two complete paths** — outcome 1 (`--continue` works) and outcome 2 (`--resume <claude_session_id>` is required, plus DB capture of Claude's internal session id from `SystemInit`). The developer runs the Step 0 probe at implementation time and picks one path without re-litigating design. Outcome 3 (neither flag works) is `BLOCKED` per the ticket; no design needed.

This is a load-bearing change because it is the second public lifecycle method on `SessionManager` (next to `new_session`); the design must keep that surface symmetric and reuse the `_teardown_locked` / `_db_insert_session` / `_consume_events` plumbing already proven by `new_session`.

## File layout

### Shared between outcomes

- `src/ccr/claude/process.py` — **modify** — `__init__` gains keyword-only `resume: bool | str = False`. `start()` reads `self._resume` and inserts the appropriate flag(s) into argv after `--verbose` and before `claude_extra_args` (see "Public surface / argv ordering" — the ticket text "before `--input-format`" is treated as drift; use the team-lead-brief position).
- `src/ccr/claude/manager.py` — **modify** — new public `continue_session()` method that mirrors `new_session` shape; new `SessionError` subclasses `SessionAlreadyRunningError` and `NoPriorSessionError` for typed control flow.
- `src/ccr/bot/handlers/session.py` — **modify** — new `cmd_continue` handler registered on the existing `session_router`; reuses the helpers (`_short_id`, the `(pid N)` template) already in the file.
- `tests/test_session_manager.py` — **modify** — extend with continue-session cases (driven via the fake claude shim plus a new `FAKE_CLAUDE_ARGV_FILE` env directive — see "Test surface").
- `tests/test_bot_continue.py` — **create** — `cmd_continue` handler-level tests (mirror `test_bot_session.py`'s `FakeManager` pattern).
- `tests/fakes/fake_claude.py` — **modify** — one new env directive `FAKE_CLAUDE_ARGV_FILE`: when set, the fake writes its full argv (one shell-token per line) to that path on startup before reading any other input. Pure observation hook for argv assertions; everything else is unchanged.

### Outcome 2 — additional files

- `src/ccr/db/models.py` — **modify** — add `claude_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)` to `Session`.
- `alembic/versions/0002_session_claude_id.py` — **create** — adds the column; downgrade drops it.
- `src/ccr/claude/manager.py` — **modify (additional)** — `_consume_events` gets a single new branch that, on `SystemInit` with non-None `event.session_id`, calls a new private `_db_update_claude_session_id(session_id, event.session_id)` exactly once per session (gated by `self._claude_session_id_persisted: bool`).

If outcome 1 is hit, the `Session` model and the `_consume_events` SystemInit-persist hook are **not** added; only the argv-flag, the new `continue_session()` method, and the bot handler land.

The ticket's `Files:` block lists every file above; the only architect-added scope is the `tests/fakes/fake_claude.py` env-directive extension, which is pure test plumbing that the developer cannot avoid (the existing fake does not expose argv to assertions).

## Public surface

### `src/ccr/claude/process.py`

```python
# Sketch — illustrative, not the final code.
class ClaudeProcess:
    def __init__(
        self,
        *,
        settings: Settings,
        cwd: Path | None = None,
        resume: bool | str = False,   # NEW
    ) -> None:
        # ... existing fields ...
        self._resume: bool | str = resume

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("ClaudeProcess.start() may only be called once.")
        self._started = True

        argv: list[str] = [
            self._settings.claude_bin,
            "-p",
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--verbose",
        ]
        # NEW — insert resume flag(s) after --verbose, before extras.
        if self._resume is True:
            argv.append("--continue")
        elif isinstance(self._resume, str) and self._resume:
            argv.extend(["--resume", self._resume])
        # else: False / "" → fresh session, no flag.

        extra = (self._settings.claude_extra_args or "").strip()
        if extra:
            argv.extend(shlex.split(extra))
        # ... rest unchanged ...
```

argv ordering note: The ticket text reads "appends `--continue` (bool path) or `--resume <id>` (str path) before `--input-format`" — the architect treats this as drift. The team-lead brief is correct: insert **after `--verbose` and before `claude_extra_args`**. Rationale: groups our control flag with the other fixed flags we own; keeps user-supplied `claude_extra_args` last so the user can override anything we set by appending. Functionally equivalent to inserting earlier (Claude does not care about flag order between fixed flags) — choose the position that keeps the argv cohort readable.

`isinstance(self._resume, str) and self._resume` guards against the `resume=""` / `resume=None`-as-falsey accident; `resume=False` (the default) yields no flag.

### `src/ccr/claude/manager.py`

```python
# Sketch — illustrative, not the final code.

class SessionAlreadyRunningError(SessionError):
    """Raised when continue_session is called while a session is running."""


class NoPriorSessionError(SessionError):
    """Raised when continue_session has no eligible prior Session row to resume."""


# Status set defining "finished and resumable" — see Decisions below.
_RESUMABLE_STATUSES: tuple[str, ...] = (
    SessionStatus.COMPLETED.value,
    SessionStatus.STOPPED.value,
)


class SessionManager:
    # ... existing fields ...

    async def continue_session(
        self,
        *,
        started_by_tg_user_id: int | None,
    ) -> uuid.UUID:
        """Resume the most recent finished session. Returns the NEW Session.id.

        Raises:
            SessionAlreadyRunningError: a session is currently running.
            NoPriorSessionError: no completed/stopped Session row exists, OR
                (outcome 2) the most recent eligible row has no
                ``claude_session_id`` to resume from.
            SessionError: claude binary missing, etc. (mirrors new_session).
        """
        async with self._session_lock:
            # Refuse to silently stop a running session — the user must /stop
            # or /clear first. This is the documented contract; mirrors the
            # bot reply from the ticket.
            if self._proc is not None:
                raise SessionAlreadyRunningError(
                    "Session already running. /stop first or /clear to start fresh."
                )

            # Look up the most recent finished session.
            prior_id, prior_claude_id = await self._db_lookup_most_recent_finished()
            if prior_id is None:
                raise NoPriorSessionError("No prior session to continue.")

            # Outcome 2 only — the prior row must carry a Claude-side id.
            # In outcome 1 this guard does not exist; resume=True is enough.
            # (Both branches are sketched explicitly in "Sequence walkthrough".)

            # ... mints session_id, opens log, constructs ClaudeProcess(... resume=...),
            #     calls start(), inserts Session row, spawns consumer/exit tasks,
            #     publishes session.status RUNNING. Identical scaffolding to
            #     new_session() except (a) ClaudeProcess gets a resume= kwarg
            #     and (b) no first-prompt-after-init wait (continue_session
            #     does not take a prompt argument; the resumed conversation
            #     already has context).
```

The body shares 90% of its lines with `new_session`; the remainder is the resume kwarg and the absence of the `prompt` parameter. The developer should keep them as two methods (do not factor into a shared helper yet — three concrete callers do not exist; per CLAUDE.md, prefer three similar lines over a premature abstraction).

```python
# Sketch — illustrative, not the final code.
async def _db_lookup_most_recent_finished(
    self,
) -> tuple[uuid.UUID | None, str | None]:
    """Return (id, claude_session_id) for the most recent finished session, or (None, None).

    'Finished' = status IN ('completed', 'stopped'). 'crashed' is deliberately
    excluded — see Decisions / Definition of "finished".

    Outcome 1: callers ignore the second tuple element.
    Outcome 2: callers require the second element to be non-None and treat
    a row with claude_session_id=NULL as ineligible (older row or never
    captured the id).
    """
    async with self._db_factory() as db:
        row = await db.scalar(
            select(Session)
            .where(Session.status.in_(_RESUMABLE_STATUSES))
            .order_by(Session.started_at.desc())
            .limit(1)
        )
        if row is None:
            return None, None
        # Outcome 1 path: claude_session_id column does not exist; this read
        # simply returns None for the second element.
        return row.id, getattr(row, "claude_session_id", None)
```

For outcome 2, the developer adds the `claude_session_id` column on `Session` (so `getattr` returns a real value or `None`) and threads the second tuple element through to the `ClaudeProcess(resume=prior_claude_id)` call. For outcome 1 the column does not exist; the second tuple element is always `None` and `continue_session` builds `ClaudeProcess(resume=True)`.

### Outcome 2 — `_consume_events` capture hook

Add ONE new branch alongside the existing `SystemInit` / `ResultEvent` branches in `_consume_events`. Order: log+publish first (current ordering preserved), then the new persist call only if not yet persisted for this session.

```python
# Sketch — illustrative, not the final code.
# Inside SessionManager:
#   self._claude_session_id_persisted: bool = False
#   (reset to False in continue_session() and new_session() alongside
#    self._init_event = asyncio.Event())

async def _consume_events(self) -> None:
    # ... existing setup ...
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
                # NEW (outcome 2 only).
                if (
                    not self._claude_session_id_persisted
                    and event.session_id is not None
                ):
                    self._claude_session_id_persisted = True
                    await self._db_update_claude_session_id(
                        session_id, event.session_id,
                    )
            elif isinstance(event, ResultEvent) and event.subtype == "success":
                self._saw_result_success = True
            self._schedule_last_event_at_update(session_id)
    # ... existing exception handling ...
```

```python
# Sketch — illustrative, not the final code.
async def _db_update_claude_session_id(
    self,
    session_id: uuid.UUID,
    claude_session_id: str,
) -> None:
    """Persist Claude's internal session id on the local Session row.

    Mirrors _update_last_event_at: short-lived DB session, eager commit,
    swallow exceptions with a structlog warning so a transient DB error
    does not crash the consumer. Idempotent: repeated calls (defensive)
    overwrite with the same value.
    """
    try:
        async with self._db_factory() as db:
            row = await db.scalar(select(Session).where(Session.id == session_id))
            if row is None:
                return
            row.claude_session_id = claude_session_id
            await db.commit()
    except Exception:
        log.exception(
            "session_manager.claude_session_id_update_failed",
            session_id=str(session_id),
        )
```

### `src/ccr/db/models.py` — outcome 2 only

```python
# Sketch — illustrative, not the final code.
class Session(Base):
    # ... existing columns ...
    claude_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
```

No index. The column is only ever written by `_db_update_claude_session_id` and read by `_db_lookup_most_recent_finished`; the latter already filters by `status IN (...)` and `ORDER BY started_at DESC LIMIT 1` (covered by the existing `ix_sessions_started_at_desc`).

### `alembic/versions/0002_session_claude_id.py` — outcome 2 only

```python
# Sketch — illustrative, not the final code.
revision: str = "0002_session_claude_id"
down_revision: str | None = "0001_initial"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("claude_session_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "claude_session_id")
```

The migration is round-trip safe: `claude_session_id` is nullable with no default, and existing rows simply receive `NULL` on upgrade. SQLite's `ALTER TABLE ADD COLUMN` natively supports this.

### `src/ccr/bot/handlers/session.py`

```python
# Sketch — illustrative, not the final code.
from ccr.claude.manager import (
    NoActiveSessionError,
    NoPriorSessionError,           # NEW
    SessionAlreadyRunningError,    # NEW
    SessionError,
)


@router.message(Command("continue"))
async def cmd_continue(
    msg: Message,
    session_manager: SessionManager,
    db_factory: async_sessionmaker[AsyncSession],  # noqa: ARG001 — parity with siblings
) -> None:
    """Resume the most recent finished session."""
    if msg.from_user is None:
        return
    try:
        session_id = await session_manager.continue_session(
            started_by_tg_user_id=msg.from_user.id,
        )
    except SessionAlreadyRunningError as exc:
        # Use the exception message verbatim — it is the documented reply.
        await msg.answer(str(exc))
        return
    except NoPriorSessionError as exc:
        await msg.answer(str(exc))
        return
    except SessionError as exc:
        await msg.answer(html.escape(str(exc)))
        return
    inf = await session_manager.info()
    pid = inf.get("pid")
    await msg.answer(f"Session {_short_id(session_id)} resumed (pid {pid}).")
```

The exception messages are the user-facing replies. We keep them as exception messages (rather than catching and replacing with a literal) so the same strings are reused if `SessionManager.continue_session` is ever called from another surface (e.g. the console). HTML escape is applied only to the unexpected-`SessionError` path because the message there can include `repr()` of arbitrary OS errors; the two known-shape errors carry only fixed strings.

Add `cmd_continue` to the module's `__all__`.

## Patterns and prior art

- **Reuse**: `src/ccr/claude/manager.py:150-240` — `new_session()` is the lifecycle template. `continue_session()` mirrors it: hold `_session_lock`, refuse to silently replace a running session (the only behavioural difference; `new_session` actively tears down), open a fresh `JsonlSessionLog`, instantiate `ClaudeProcess`, call `start()`, allocate a new in-memory UUID, insert the row, spawn the consumer + exit tasks, publish `session.status` RUNNING.
- **Reuse**: `src/ccr/claude/manager.py:609-637` — `_db_insert_session` is the template for the new `_db_update_claude_session_id`. Same short-lived-session shape, same exception swallow, same structlog noise.
- **Reuse**: `src/ccr/claude/manager.py:438-475` — `_consume_events` already has the SystemInit branch; the new persist hook drops in beside the existing `_init_event.set()` line.
- **Reuse**: `src/ccr/bot/handlers/session.py:56-75` — `cmd_new` is the template for `cmd_continue` (same workflow-data injection, same `info()`-then-format reply). Reuse `_short_id`.
- **Reuse**: `src/ccr/db/models.py:91-118` — `Session` already has `String(16)` for status and timezone-aware datetimes. The new column matches the existing `Text` columns (`exit_reason`, `tg_username`).
- **Reuse**: `alembic/versions/0001_initial.py` is the migration shape: synchronous `op.create_*` / `op.drop_*` calls, no async. Outcome 2's migration is a 3-line `add_column` / 2-line `drop_column`.
- **Avoid**: factoring `new_session` and `continue_session` through a shared internal helper. Two callers in one file with mostly-identical bodies is preferred to a premature abstraction (per CLAUDE.md: "three similar lines is better than a premature abstraction").
- **Avoid**: holding the `claude_session_id` in a `SessionManager` field as a "last seen" cache to read in `continue_session`. The DB is the durable source of truth (the bot may restart between sessions); read from the DB inside `continue_session()` rather than maintaining an in-memory mirror that is wrong after every restart.
- **Avoid**: building a JSONL-replay fallback for outcome 3. The ticket explicitly forbids it; the architect-and-developer escalation path is `BLOCKED`.

## Abstractions

**No new abstraction.** The ticket adds a second public lifecycle method (`continue_session`) and one new private DB helper (`_db_update_claude_session_id`, outcome 2 only). Two new `SessionError` subclasses (`SessionAlreadyRunningError`, `NoPriorSessionError`) are not abstractions — they are typed control-flow markers that let the bot handler match by class instead of string-matching exception messages. They cost ~6 lines of code total and prevent a fragile string match.

`continue_session` and `new_session` end up ~95% structurally identical. Resist the temptation to factor them into a shared `_spawn_session(resume_arg, prompt)` helper now: today there are exactly two callers, and `new_session`'s extra `prompt` handling (with `_init_event.wait`) makes the shared helper sprout an awkward optional argument. Revisit if a third caller arrives (e.g. a future `/fork` or scheduled-restart subsystem).

## Sequence walkthrough

### Outcome 1 — `--continue` works in `-p` stream-json mode

```
User taps /continue
  ↓
cmd_continue (handlers/session.py)
  ↓
SessionManager.continue_session(started_by_tg_user_id=42)
  ├─ acquire _session_lock
  ├─ if _proc is not None → raise SessionAlreadyRunningError
  ├─ _db_lookup_most_recent_finished()
  │     └─ SELECT * FROM sessions WHERE status IN ('completed','stopped')
  │        ORDER BY started_at DESC LIMIT 1
  │     returns (prior_id, _) or (None, _)
  ├─ if prior_id is None → raise NoPriorSessionError
  ├─ session_id = uuid.uuid4()
  ├─ session_log = JsonlSessionLog(<data>/logs/{session_id}.jsonl); open()
  ├─ proc = ClaudeProcess(settings=..., resume=True)         # ← outcome 1
  ├─ proc.start()                                            # argv has --continue
  ├─ assign self._proc, self._session_id, self._log, _status = RUNNING
  ├─ self._db_insert_session(session_id, started_at, started_by_tg_user_id=42, first_prompt=None)
  ├─ spawn _consume_events / _await_exit tasks
  ├─ bus.publish("session.status", {RUNNING})
  └─ release lock; return session_id
  ↓
cmd_continue replies "Session abc12345 resumed (pid 4242)."
```

No SystemInit DB write. No `claude_session_id` column. The conversation context is restored by Claude itself via its own local conversation persistence (under `~/.claude/`).

### Outcome 2 — `--resume <claude_session_id>` is required

```
User taps /continue
  ↓
cmd_continue (handlers/session.py)
  ↓
SessionManager.continue_session(started_by_tg_user_id=42)
  ├─ acquire _session_lock
  ├─ if _proc is not None → raise SessionAlreadyRunningError
  ├─ _db_lookup_most_recent_finished()
  │     └─ SELECT id, claude_session_id FROM sessions
  │        WHERE status IN ('completed','stopped')
  │        ORDER BY started_at DESC LIMIT 1
  │     returns (prior_id, prior_claude_id) | (None, None)
  ├─ if prior_id is None or prior_claude_id is None
  │     → raise NoPriorSessionError("No prior session to continue.")
  ├─ session_id = uuid.uuid4()
  ├─ session_log = ... open()
  ├─ proc = ClaudeProcess(settings=..., resume=prior_claude_id)   # ← outcome 2
  ├─ proc.start()                                                # argv has --resume <id>
  ├─ self._claude_session_id_persisted = False                    # reset for new session
  ├─ assign state, insert Session row (with claude_session_id=NULL initially)
  ├─ spawn _consume_events / _await_exit
  ├─ bus.publish("session.status", {RUNNING})
  └─ release lock; return session_id
  ↓
cmd_continue replies "Session abc12345 resumed (pid 4242)."

Asynchronously:
  Claude emits {"type":"system","subtype":"init","session_id":"<new-claude-id>"}
  ↓
  _consume_events:
    seq = log.append(event)
    bus.publish(...)
    if SystemInit:
      _init_event.set()
      if not _claude_session_id_persisted and event.session_id is not None:
        _claude_session_id_persisted = True
        await _db_update_claude_session_id(session_id, event.session_id)
        # Row updated: sessions[session_id].claude_session_id = "<new-claude-id>"
```

Chaining: the next `/continue` looks up THIS row (now status=`completed`/`stopped` after exit), reads its newly-populated `claude_session_id`, and threads it back as `--resume <new-claude-id>`. The chain works as long as every link captures its own `claude_session_id` from `system.init`.

### Both outcomes — error paths

| Trigger | Exception | Bot reply |
|---|---|---|
| `_proc is not None` | `SessionAlreadyRunningError` | `"Session already running. /stop first or /clear to start fresh."` |
| No matching row in DB | `NoPriorSessionError` | `"No prior session to continue."` |
| (Outcome 2) row has `claude_session_id IS NULL` | `NoPriorSessionError` | `"No prior session to continue."` |
| `claude_bin` missing / `start()` fails | `SessionError` | `html.escape(str(exc))` |

The "no claude_session_id on the row" reply is intentionally the same as "no row at all" — from the user's perspective, both mean "this bot has no resumable history". A user who just paired and has only crashed sessions sees the same message as a user with an empty DB.

## Decisions

### `resume` parameter lives on `__init__`, not `start()`

The brief asks: "does `start()` read it from `self` or is it passed to `start()` directly?" Decision: stash on `__init__`, read in `start()`. Two reasons:

1. The current `start()` takes no args. Adding `start(resume=...)` would break the existing single call site in `new_session` (which would have to pass `resume=False` defensively). Keeping `start()` signature stable means `new_session` is unchanged.
2. `ClaudeProcess` is a one-shot object (already documented: `start()` may only be called once). Configuration passed at construction time is symmetric with `settings` and `cwd` already on `__init__`.

`SessionManager.new_session()` keeps `ClaudeProcess(settings=self._settings)` — the default `resume=False` means no flag. `SessionManager.continue_session()` passes `resume=True` (outcome 1) or `resume=<claude_session_id>` (outcome 2).

### argv insertion site

After `--verbose`, before `claude_extra_args`. The ticket text "before `--input-format`" is treated as drift — both positions are functionally equivalent (Claude does not care about flag order) but "after `--verbose`" keeps our control flags grouped and lets `claude_extra_args` (the user's escape hatch) genuinely come last. Document this disagreement in the developer's work summary.

### "Most recent finished" definition

`status IN ('completed', 'stopped')`. **Crashed sessions are excluded.** Rationale:

- The ticket's smoke test path is `/new → /stop → /continue`, which produces `stopped`. So `stopped` must be in the set.
- `completed` (Claude exited cleanly with a successful `result` event) is the canonical happy-path finished state. Must be in the set.
- `crashed` is excluded because a crashed session may have left Claude's local conversation cache in a partial / inconsistent state. Resuming a crashed session is unsafe; if a user really wants to do this, the future `/continue <id>` follow-up can give them an explicit override.
- `running` is excluded by definition (CCR-019 reconciliation guarantees `status='running'` rows reflect a live process at startup, and inside `continue_session` we have already refused to act if `_proc is not None`).

If outcome 2 lands and the most recent finished row has `claude_session_id IS NULL` (e.g. it was created before the migration, or `system.init` never fired), it's treated as "no prior session" — same reply, same UX. The user can `/new` to start fresh.

### `SystemInit.session_id` field name

The brief calls out: `SystemInit` already models `session_id: str | None = None` (events.py:86). Decision: this is the canonical extractor. The developer's probe must confirm the real Claude binary populates this field on `system.init` (the field is documented in stream-json upstream; the existing fake fixture also uses it — `tests/test_session_manager.py:140`). If the real field name turns out to be different, the architect's `_consume_events` hook is wrong by exactly one attribute name, which the developer can fix locally without re-litigating the design.

If `SystemInit.session_id is None` on every event from a real Claude session, treat outcome 2 as outcome 3 (BLOCKED) — there is no Claude id to capture, so `--resume` would have nothing to resume.

### `NoPriorSessionError` covers both "no row" and "row missing claude_session_id"

The bot reply for both cases is `"No prior session to continue."` (per ticket acceptance). Modelling them as one exception keeps the handler trivially readable; a user does not need to distinguish "no history" from "history but no resume id".

### Persist-once flag

`_claude_session_id_persisted: bool` — reset to `False` at the top of `new_session` and `continue_session`. We persist on the FIRST `SystemInit` event of a session and skip subsequent ones. Two reasons:

1. A session emits exactly one `system.init` in normal operation; defending against an upstream regression (multiple init events) is cheap and avoids a race where two consumer-task iterations both try to write.
2. The `claude_session_id` of a session does not change mid-session, so overwriting with the same value is safe but wasteful.

### One commit, two outcomes

The developer lands ONE outcome based on the probe. The architect designs both fully so there is no design debt either way.

If outcome 1: skip the `Session.claude_session_id` column, skip the migration, skip the `_consume_events` persist hook, but keep the `(prior_id, prior_claude_id)` tuple shape on `_db_lookup_most_recent_finished` (the second element is always `None`; trivial). This makes upgrading to outcome 2 later (if Claude tightens the constraint) a one-migration, one-hook change.

If outcome 2: full design, including the migration. The developer must also add a test that asserts `SystemInit` causes a DB write (covered in Test surface).

If outcome 3: return `BLOCKED — Claude Code -p mode does not support continuation; awaiting upstream`. Do not improvise a JSONL-replay fallback.

## Dependencies

- Depends on:
  - `src/ccr/claude/process.py` — `ClaudeProcess.__init__` / `start()` argv builder.
  - `src/ccr/claude/manager.py` — `_session_lock`, `_db_insert_session`, `_consume_events`, `_await_exit`, `JsonlSessionLog` setup; reuse-by-mirroring for the new method.
  - `src/ccr/db/models.py` — `Session` row shape (outcome 2 adds a column).
  - `src/ccr/claude/state.py` — `SessionStatus.COMPLETED`, `SessionStatus.STOPPED` for the resumable-status set.
  - `src/ccr/claude/events.py` — `SystemInit.session_id` (outcome 2 only).
  - `ccr.bot.handlers.session.router` — existing `session_router`; `cmd_continue` registers on it.
- Used by:
  - `cmd_continue` (this ticket).
  - Anyone driving `SessionManager` directly (the console; future automation hooks).
  - The chained behaviour: future `/continue` calls reading the row this session writes (outcome 2).
- Indirect dep on CCR-019 (orphan reconciliation): without it, `continue_session` could pick a `status='running'` row that isn't actually running. CCR-019 has already landed (see `_RESUMABLE_STATUSES` excludes `running` defensively anyway).

## Edge cases the developer must handle

- **Already-running guard.** `if self._proc is not None: raise SessionAlreadyRunningError(...)`. Tests must cover this path; the bot handler must reply with the literal string `"Session already running. /stop first or /clear to start fresh."`.
- **Empty DB / new install.** `_db_lookup_most_recent_finished` returns `(None, None)` → `NoPriorSessionError`. Bot reply: `"No prior session to continue."`.
- **Outcome-2 row with NULL claude_session_id.** Same `NoPriorSessionError`, same reply. The developer should write this as a unit test for outcome 2 specifically.
- **Crashed-only history.** If every prior row is `crashed`, `_db_lookup_most_recent_finished` returns `(None, None)` because `crashed` is excluded from `_RESUMABLE_STATUSES`. Reply: `"No prior session to continue."`. Decision documented above.
- **`claude_bin` missing.** `proc.start()` raises `FileNotFoundError`; `SessionError` is raised exactly as in `new_session`. Bot reply uses `html.escape(str(exc))`.
- **`SystemInit` never arrives (timeout) — outcome 2.** No DB write happens; the new session's row stays with `claude_session_id=NULL`. Next `/continue` will see it as ineligible and reply `"No prior session to continue."` — until the user runs a successful session. This is acceptable: a session that never sees init is broken anyway, and the chain reseeds once a healthy session lands.
- **`_consume_events` exception during the new persist call.** `_db_update_claude_session_id` swallows exceptions with a structlog noise line (mirrors `_update_last_event_at`). The session continues; the chain will be broken at this link but recovers on the next session.
- **Race: `continue_session` and a `/stop` of an idle session.** Both acquire the same `_session_lock`. `/stop` sees `_proc is None` and is a no-op; `continue_session` proceeds normally. No data race.
- **Race: `continue_session` and `new_session` called concurrently.** Both acquire `_session_lock`; whichever runs first wins, the second sees `_proc is not None` and either tears down (`new_session`) or raises (`continue_session`). The race outcome is well-defined.
- **Migration round-trip (outcome 2).** `alembic upgrade head` adds the column. `alembic downgrade -1` drops it. Existing rows on upgrade get NULL; no data loss either direction. Test in CI via the same harness as `0001_initial`.
- **Concurrency with broadcast/SSE.** No interaction. `continue_session` publishes the same `session.status` and `session.event` topics the broadcast loop and SSE already consume. The buffer-and-pause logic (CCR-009) is per-session-id and has no special case for resumed sessions.
- **Owner-model interaction.** `started_by_tg_user_id` is recorded on the new row from the `/continue` caller. The `last_chat_id` of the original session author is irrelevant; broadcasts go to all paired users with `last_chat_id` (existing CCR-008 behaviour).
- **HTML escaping.** The bot handler passes the literal exception message for the two known errors (already-running, no-prior); both are fixed strings with no user-controlled interpolation, so no escape needed. Only the catch-all `SessionError` path uses `html.escape` (covers e.g. paths with `<` in them from a `FileNotFoundError`).

## Test surface

### `tests/fakes/fake_claude.py` (modify)

Add one env directive `FAKE_CLAUDE_ARGV_FILE`. When set, the fake writes `"\n".join(sys.argv) + "\n"` to that path before reading any input. ~5 lines. This lets the SessionManager tests assert which flag was passed. No other behaviour changes.

```python
# Sketch — illustrative.
argv_file = os.environ.get("FAKE_CLAUDE_ARGV_FILE")
if argv_file:
    Path(argv_file).write_text("\n".join(sys.argv) + "\n", encoding="utf-8")
```

### `tests/test_session_manager.py` (modify)

Shared scenarios (both outcomes):

- `tests/test_session_manager.py::test_continue_session_passes_resume_flag_to_subprocess` — outcome 1: prior session is `completed`; call `continue_session`; read the argv-file written by `FAKE_CLAUDE_ARGV_FILE`; assert `--continue` is present. Outcome 2: assert `--resume` and the captured Claude id are present.
- `tests/test_session_manager.py::test_continue_session_with_no_prior_session_raises` — empty DB; assert `NoPriorSessionError`.
- `tests/test_session_manager.py::test_continue_session_while_running_raises` — start a session, do not stop it, call `continue_session`; assert `SessionAlreadyRunningError`.
- `tests/test_session_manager.py::test_continue_session_picks_most_recent_finished` — seed three rows: oldest=completed, middle=crashed, newest=stopped. Call `continue_session`; assert the resume target was the newest (stopped) row, NOT the middle (crashed) one. Smoking-gun for the `crashed` exclusion.
- `tests/test_session_manager.py::test_continue_session_inserts_new_row_with_running_status` — call `continue_session`; assert a NEW `Session` row is inserted with `status='running'` (the prior row is unchanged).

Outcome 2 only:

- `tests/test_session_manager.py::test_system_init_persists_claude_session_id` — drive a fake claude that emits `{"type":"system","subtype":"init","session_id":"claude-abc"}`; after the session reaches COMPLETED, assert the `Session.claude_session_id` row column is `"claude-abc"`.
- `tests/test_session_manager.py::test_continue_session_with_null_claude_session_id_raises` — seed a `completed` row with `claude_session_id=NULL`; call `continue_session`; assert `NoPriorSessionError`.
- `tests/test_session_manager.py::test_system_init_persists_claude_session_id_only_once` — fake emits two `system.init` events with different `session_id` values; assert the row is written exactly once (first wins, second is no-op). Verify by patching `_db_update_claude_session_id` or by reading the row after the run.

### `tests/test_bot_continue.py` (create)

Mirror `tests/test_bot_session.py`'s pattern: in-memory DB factory + `FakeManager` stub. Three tests:

- `tests/test_bot_continue.py::test_cmd_continue_happy_path_replies_resumed` — fake manager returns a `session_id`; assert reply matches `re.match(r"^Session [0-9a-f]{8} resumed \(pid \d+\)\.$", reply)`.
- `tests/test_bot_continue.py::test_cmd_continue_already_running_replies_canned_string` — fake manager raises `SessionAlreadyRunningError`; assert reply is exactly `"Session already running. /stop first or /clear to start fresh."`.
- `tests/test_bot_continue.py::test_cmd_continue_no_prior_session_replies_canned_string` — fake manager raises `NoPriorSessionError`; assert reply is exactly `"No prior session to continue."`.
- `tests/test_bot_continue.py::test_cmd_continue_other_session_error_html_escapes` — fake manager raises `SessionError("claude binary not found: </path>")`; assert reply is HTML-escaped (`&lt;path&gt;`).

The `FakeManager` from `test_bot_session.py` has a `new_session` AsyncMock; extend it with a `continue_session` AsyncMock following the same pattern. (Either copy the helper or import it from the sibling test module — copying is fine for a fresh test file.)

### Coverage budget

The new code is ~80 lines (outcome 1) or ~140 lines (outcome 2). The tests above exercise every branch (happy, already-running, no-prior, missing-claude-id-outcome-2, crash-exclusion, html-escape on unexpected errors, argv flag plumbing). Coverage should stay above the 80% gate without surprise; the fake-claude argv-file directive needs `FAKE_CLAUDE_ARGV_FILE` to be exercised at least once (every continue test sets it).

## Out of scope

- Picking which prior session to continue when there are several. Default is "most recent" (oldest stopped/completed if newest is crashed). An explicit `/continue <id>` UI is a separate follow-up ticket.
- A `/resume <id>` selector on the `/sessions` listing.
- JSONL-replay fallback for outcome 3 (the ticket explicitly forbids it; escalate via BLOCKED).
- Reconciling crashed sessions — `crashed` is deliberately excluded from the resumable-status set; if a user wants to resume a crashed session, that is a future feature with its own UX.
- Concurrency safety against multiple paired users tapping `/continue` simultaneously beyond the existing `_session_lock` (which already serialises lifecycle transitions).
- Web viewer / SSE: no work here. The new session emits the same events on the same bus topics; the viewer renders it identically to a fresh session.
- Changing the argv-flag insertion contract for any other resume modality (e.g. `--continue <prompt>`) — outside the ticket's scope.
- Cleaning up the misattributed `TODO(CCR-019)` in `process.py:152` — that work belongs to CCR-021.
- Updating `CONTEXT.md` / `BRIEF.md` — team-lead's job after `APPROVED`.

## Open questions for team lead

None. The brief's five questions are answered above:

1. `resume` parameter location → `__init__`, read in `start()` from `self._resume`.
2. `_consume_events` capture path → new private `_db_update_claude_session_id` helper, gated by a `_claude_session_id_persisted` flag, fired from inside the existing `SystemInit` branch BEFORE setting `_init_event` if non-None.
3. `continue_session` signature → exactly as the ticket specifies; raises `SessionAlreadyRunningError` and `NoPriorSessionError` (new `SessionError` subclasses) for the two canned-reply paths and bare `SessionError` for everything else; returns the new local `uuid.UUID`.
4. "Most recent finished" predicate → `status IN ('completed', 'stopped')`; `crashed` deliberately excluded.
5. `claude_session_id` source → DB query inside `continue_session()` (not in-memory); the new row also captures its OWN `claude_session_id` via the `_consume_events` hook so the chain works.

The architect explicitly accepts the ticket's "before `--input-format`" wording as drift and recommends the team-lead-brief position ("after `--verbose`, before `claude_extra_args`"). If the team lead disagrees, the change is one line in `start()` and does not affect any other decision.
