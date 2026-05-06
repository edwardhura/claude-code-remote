# Brief: claude-runtime

## Purpose
The subprocess + event infrastructure that every higher-level feature depends on. Owns the managed Claude Code subprocess (`ClaudeProcess`), the in-process pub/sub fan-out (`EventBus`), the discriminated-union event schema (`ClaudeEvent`), the append-only per-session log (`JsonlSessionLog`), and the lifecycle FSM (`SessionManager`) that wires them together. Also owns the in-process MCP permission server that gates Claude tool calls behind Telegram inline buttons, and the fake-claude test harness that lets the rest of the codebase run end-to-end tests without the real `claude` binary.

## Key invariants
- **One running Claude session at a time, globally.** `SessionManager` enforces this with a single asyncio lock; `started_by_tg_user_id` records who kicked it off but any paired user can interact and any paired user receives broadcasts.
- **Log-before-publish ordering.** Every event is `JsonlSessionLog.append()`-ed before being published on the `EventBus`. This is what makes SSE `?from=<seq>` replay-then-tail correct: an SSE subscriber that joins after a publish still sees the event, because the log already contains it.
- **JSONL `seq` is derived from line count**, not stored in SQL. The Nth line in `data/logs/<session_id>.jsonl` has `seq = N`.
- **Drift tolerance via `UnknownEvent`.** `ClaudeEvent` is a *plain* `Union[_KnownEvent, UnknownEvent]` — `_KnownEvent` is the discriminated union; `UnknownEvent` catches schema drift. `parse_event()` has a double fallback so unknown variants never raise.
- **`McpPermissionRequest` is a synthetic bus envelope, NOT a `ClaudeEvent` member.** The MCP permission server publishes it on the bus; it does not live in the JSONL stream.
- **`claude_session_id` is captured from the first `SystemInit` event and persisted exactly once per session lifetime.** `_claude_session_id_persisted` flag is the single-shot guard. Persistence is fire-and-forget via `asyncio.create_task`; task ref held in `_claude_session_id_pending` for teardown drain. The write happens AFTER `log.append` and `bus.publish` for `SystemInit`.
- **Resume goes through `claude --resume <claude_session_id>`.** A row with `claude_session_id IS NULL` is NOT resumable; both `_db_lookup_resumable_claude_session_id` and the no-prefix path raise `NoPriorSessionError`.
- **Multiple `Session` rows may share the same `claude_session_id`** — this is the going-forward shape for `--resume` chains (each resume inserts a fresh row pointing at the prior conversation's Claude session id). Code that scans by `claude_session_id` must tolerate duplicates; lookups already use `started_at desc → first non-null` which handles this naturally.
- **`import_claude_session`'s double-import guard is programmatic (SELECT-before-INSERT in `src/ccr/claude/import_session.py`), NOT a DB-level UNIQUE constraint.** Future code that adds a second insert path for `claude_session_id` must perform its own duplicate check; the DB index `ix_sessions_claude_session_id_not_null` is non-unique and exists only for query speed.
- **Imported rows are inserted with `status="stopped"` (not `completed`)** — the foreign session cannot be proven to have reached a successful `result` event.
- **Imported rows do NOT write `first_prompt` or `name`** — those are session-side concerns; CCR-037 owns `name`.
- **`claude_session_id` arguments are validated against a strict UUID4 regex inside `find_claude_session_file` BEFORE any filesystem path is constructed**; non-UUID4 input raises `ImportSessionError("Invalid Claude session id format")`.
- **Subprocess shutdown is SIGTERM → grace timeout → SIGKILL**, both `start()` and `stop()` are idempotent. Stderr is captured into an 8192-byte ring buffer.
- **Synthetic crash event.** If the subprocess exits abnormally, `SessionManager` synthesises a crash event onto the bus (and into the JSONL) before transitioning the `Session` row to `CRASHED`.
- **MCP socket is owner-only.** The Unix socket the relay subprocess connects to is `chmod 0o700` immediately after creation.
- **Settings → argv flag positions are pinned.** `permission_mode`, `allowed_tools`, `disallowed_tools` slot between resume/continue and the MCP block; `claude_extra_args` is whitespace-split via `shlex` and appended last. Tests pin the order.
- **Telegram broadcast pauses on a pending permission**, but **SSE keeps streaming live.** While a permission request is outstanding, the bot's broadcast task buffers events for that session and resumes when a paired user taps a button. Web subscribers see everything in real time.

## Public surface
### Events bus (`src/ccr/events/`)
- `events/__init__.py` — re-exports `EventBus`, `Subscriber`.
- `events/bus.py::EventBus` — WeakSet-tracked subscriber queues; snapshot-iterating `publish()`; drop-oldest backpressure with a WARN log; `subscribe()` is an async-generator with explicit `finally` cleanup.

### Event schema (`src/ccr/claude/events.py`)
- `ClaudeEvent` — plain `Union[_KnownEvent, UnknownEvent]`; `_KnownEvent` is a discriminated union on `type` covering `SystemInit`, `AssistantTurn`, `ToolUseBlock`, `ToolResultBlock`, `ResultEvent`, `RateLimitEvent`, plus the `UserTurn` echo.
- `parse_event(line: bytes | str) -> ClaudeEvent` — double-fallback parser; never raises on schema drift.
- `ContentBlock` — inner/outer variants (text, thinking, tool_use, tool_result).
- `ResultEvent` — has optional `usage: ResultUsage` sub-model; `RateLimitEvent` carries `RateLimitInfo` with camelCase aliases (`populate_by_name=True`, `extra="allow"`).
- `SystemInit.skills: list[str]` — surfaced from the `system/init` JSONL.
- `McpPermissionRequest` — synthetic bus envelope (not a `ClaudeEvent` discriminator member); produced by the MCP server.

### Subprocess (`src/ccr/claude/process.py`)
- `ClaudeProcess` — manages the `claude` child. `start(*, resume: bool | str = False, mcp_argv: Sequence[str] | None = None)`, idempotent `stop()`, line-buffered async generator over stdout, stderr ring buffer (8192 bytes), `send_user_turn(text)`, public read-only `pid` property. argv builder reads `permission_mode` / `allowed_tools` / `disallowed_tools` from Settings.
- `ClaudeProcess(resume=<str>)` — non-empty string emits `--resume <str>`; `resume=True` still emits `--continue`.

### JSONL log (`src/ccr/claude/log.py`)
- `JsonlSessionLog` — `append(event)`, `read_from(seq)`, `tail()` (`asyncio.Event`-notified). Module-level `prune()` for retention-count and age-based cleanup. Parent dir auto-created on first write.

### MCP permission server (`src/ccr/claude/mcp.py` + `src/ccr/claude/mcp_relay.py`)
- `McpPermissionServer` — in-process MCP server exposing the `ccr_permission_prompt` tool; `request_id → asyncio.Future` map; deterministic `.ccr-mcp-config.json` config file; `start()` / `stop()` lifecycle; public `resolve(request_id, decision)`, `cancel_pending()`, `is_pending(request_id)`; `claude_argv` property.
- `set_suppressed_tool_handler(cb)` + `suppressed_tool_names: frozenset[str]` — when a suppressed tool name (e.g. `AskUserQuestion`) hits `_on_tool_call`, the bus publish is skipped and the handler is invoked with `(request_id, tool_name, tool_input)` instead.
- `_handle_relay_connection` — frames newline-delimited JSON-RPC bytes from the relay's Unix socket into MCP `SessionMessage`s; runs three concurrent tasks (socket reader, socket writer, `Server.run`) under `anyio.create_task_group`.
- `mcp_relay.py` — ~35-LOC stdio↔Unix-socket relay subprocess that `claude` spawns per `--mcp-config`.
- `McpServerStartError`.

### Session manager (`src/ccr/claude/manager.py`)
- `SessionManager` — single asyncio lock, lifecycle FSM, log-before-publish ordering, synthetic crash event. Exposes `new_session(...)`, `continue_session(*, started_by_tg_user_id, session_id_prefix=None)`, `stop()`, `send_user_turn`, `send_tool_result(tool_use_id, content, *, is_error=False)`, `send_slash`, `info()` snapshot (`{session_id, pid, started_at, status}`), `running_subagents()`, `available_skills()`, `is_session_active()`, `current_session_usage()`, `current_rate_limit_status()`, `resolve_permission()`, `is_permission_pending()`, `shutdown()`.
- `SessionManager._db_lookup_resumable_claude_session_id(*, session_id_prefix=None) -> str | None` — replaces `_db_lookup_most_recent_finished` and `_db_lookup_session_by_prefix`. Returns the `claude_session_id` to pass to `claude --resume <id>`. With prefix: raises `SessionNotFoundError` if no row matches; raises `NoPriorSessionError` if the matched row's `claude_session_id IS NULL`.
- AskUserQuestion bookkeeping: `_PendingQuestion` dataclass (with `mcp_request_id: str | None`), `_pending_questions: dict[str, _PendingQuestion]`, timeout tasks per pending question, `_unpaired_mcp_auq_calls: deque[str]` for MCP-first race ordering, `_on_mcp_ask_user_question` handler.
- Errors: `SessionError`, `NoActiveSessionError`, `SessionAlreadyRunningError`, `NoPriorSessionError`, `SessionNotFoundError` (the dead `StaleSessionError` / `StaleToolUseError` were removed in CCR-033).
- `SessionStatus(StrEnum)` (`src/ccr/claude/state.py`): `IDLE`, `RUNNING`, `COMPLETED`, `STOPPED`, `CRASHED`.

### DB model extension (`src/ccr/db/models.py`)
- `Session.claude_session_id: str | None` — Claude's own `session_id` (UUID-shaped string Claude reports in its `system.init` event). Non-unique partial index `ix_sessions_claude_session_id_not_null` on non-NULL values (unique flag dropped in CCR-041; multiple rows may share the same value in resume chains).

### Import session (`src/ccr/claude/import_session.py`)
- `import_claude_session(db, claude_session_id, *, projects_root=None) -> Session` — reads Claude's local session file and inserts a `Session` row with `status="stopped"`, `started_at` from the first event timestamp, and `started_by_tg_user_id` from the owner row. Does NOT copy JSONL into `data/logs/`.
- `find_claude_session_file(claude_session_id, *, projects_root=None) -> Path` — scans `<projects_root>/*/` for `<id>.jsonl`; validates input against a strict UUID4 regex before any filesystem path construction.
- `encoded_cwd_for(path) -> str` — encodes a path for use as a Claude projects directory name.
- Exceptions: `ImportSessionError`, `ClaudeSessionFileNotFoundError`, `DuplicateClaudeSessionError`, `NoOwnerError`.

### Usage (`src/ccr/claude/usage.py`)
- `SessionUsage` frozen dataclass + `aggregate_session_usage(jsonl_path)` walker. Aggregates input/output/cache tokens, tool-use count, success-turn count, total elapsed (with `duration_api_ms` fallback), and total cost USD across every `result` event. Forward-compatible: `UnknownEvent` lines and parse errors fall through silently.

### Settings extensions (cross-listed from `core`)
- `mcp_permission_timeout_seconds: int = 120` (1..3600).
- `permission_mode: Literal[...] | None`, `allowed_tools: list[str]`, `disallowed_tools: list[str]` (CSV env parser, mutual-exclusivity validator).
- `ask_user_question_timeout_seconds: int = 600`.

### Test harness (`tests/fakes/`)
- `fake_claude.py` — env-var-driven JSONL emitter replacing the real claude binary; supports `FAKE_CLAUDE_ARGV_FILE` directive for argv recording.
- `fake_claude` — POSIX shell shim (0o755) invoking the Python script.

## Subtleties / gotchas
- **MCP relay is a subprocess, not in-process.** `claude` spawns `python -m ccr.claude.mcp_relay <socket_path>`; that subprocess connects back to the in-process MCP server over a Unix socket. Don't try to make MCP fully in-process — `claude` requires a stdio-speaking `--mcp-config` entry.
- **AskUserQuestion is double-suppressed at MCP source.** Suppressing at the bus would race the formatter; suppressing only at the formatter would let stale `AskUserQuestion` envelopes leak. Source suppression is primary; the formatter has a defensive `_AUQ_SUPPRESSED_TOOL_NAMES` guard for schema drift / test fixtures.
- **MCP-first race for AUQ.** When the MCP `AskUserQuestion` request arrives before the Claude-side `tool_use` block is parsed, the request_id is queued in `_unpaired_mcp_auq_calls`; the matching `_track_ask_user_question` drains the deque and pairs by FIFO order.
- **`send_tool_result` for MCP-paired AUQ resolves via `deny + message`** (not `_deliver_tool_result`) — claude's harness then writes the synthetic tool_result, eliminating a double-tool_result race that bit CCR-028.
- **Log-before-publish requires the writer to be sync-correct.** The `JsonlSessionLog.append` writes the line and then sets the `asyncio.Event` that wakes tailers. Don't introduce a "publish first, log after" optimisation — SSE replay would lose events.
- **`UnknownEvent` is a feature, not a bug.** When Claude Code stream-json schema drifts, the runtime keeps streaming. Tests pin the fallback paths.
- **Settings argv flags have a pinned slot order.** Resume/continue → `permission_mode`/`allowed_tools`/`disallowed_tools` → MCP block → `claude_extra_args` last. Tests in `test_claude_process.py` and `test_config.py` enforce this.
- **`shutdown()` is the explicit teardown path.** `server.py::serve` calls `manager.shutdown()` in its finally; this cancels pending question timeouts and stops the MCP server. Do not skip it on normal exit.
- **`/clear` divider is gated on `prior_status != IDLE`** (chat-bot owns the gate, but the read happens before `manager.stop()`). Idle clears stay quiet.
- **macOS path canonicalisation (`/tmp` → `/private/tmp`) breaks `encoded_cwd_for(os.getcwd())` round-tripping.** `find_claude_session_file` therefore scans every `<projects_root>/*/` for `<id>.jsonl` rather than constructing the path directly.
- **The partial index `ix_sessions_claude_session_id_not_null` is non-unique (CCR-041)**; the `WHERE claude_session_id IS NOT NULL` predicate is preserved so the index still serves resume-time lookups by Claude session id.
- **`SessionManager._update_claude_session_id` is fire-and-forget with `try/except Exception → log.exception`**; the `IntegrityError` path that pre-CCR-041 fired on every `/continue` no longer occurs. Any future `claude_session_id_update_failed` log entry now indicates a real DB fault, not the resume-chain duplicate-write.
- **The first JSONL line in some Claude sessions is `type:"file-history-snapshot"` whose `timestamp` is nested.** The import only honours top-level `timestamp` keys; if none is found, `started_at` falls back to `datetime.now(UTC)` and a `import_session.no_timestamp` warning is logged.
- **`import_claude_session` does NOT copy Claude's JSONL into `data/logs/`.** Claude owns its history. Our JSONL is only created when a continuation streams.

## Cross-feature relations
- depends on: core (Settings, DB models, async engine, logging); auth (`ccr.auth.pairing.get_owner` used by `import_claude_session`).
- used by: chat-bot (every command and the broadcast loop), web-viewer (planned — SSE replay + tail); `ccr.cli session save` and `ccr.console.app session save` call into `ccr.claude.import_session`.

## Status
- State: IN PROGRESS
- Tickets: CCR-007, CCR-018 (cross-listed UX touch), CCR-024, CCR-025, CCR-028 (relay bridge), CCR-028 (AUQ collision), CCR-029, CCR-030 (SystemInit.skills + accessors), CCR-032 (RateLimitEvent + accessor), CCR-033 (cleanup), CCR-036, CCR-041
- Last updated: CCR-041 (2026-05-06)
