# Tickets

Append-only ticket log. Status lives in the title for grep-ability:
```
grep -E '^## CCR-[0-9]+' TICKETS.md
```

---

## CCR-001: Python package scaffold [todo]
Phase: 1
Feature: core
Files:
  - `pyproject.toml` — project metadata, deps, scripts, ruff/mypy config.
  - `uv.lock` — generated.
  - `.gitignore` — Python, IDE, `data/`, `.env`, `.venv/`.
  - `src/ccr/__init__.py` — exposes `__version__`.
  - `src/ccr/__main__.py` — `from ccr.cli import main; main()`.
  - `src/ccr/cli.py` — argparse skeleton with `serve`, `console`, `pair {list,pending,approve,revoke,invite}`, `doctor`, `init-db` subcommands stubbed (NotImplementedError bodies).
  - `tests/test_cli.py` — `--help` exits 0; unknown subcommand exits 2.
Out of scope:
  - Any business logic, DB, env loading.
Acceptance:
  - [ ] `uv sync` succeeds.
  - [ ] `python -m ccr --help` exits 0 and lists `serve`, `console`, `pair`, `doctor`, `init-db`.
  - [ ] `python -m ccr pair list` exits with `NotImplementedError` (proves dispatch works).
  - [ ] `ruff check src tests` passes.
  - [ ] `mypy src` passes.
  - [ ] `pytest` runs `test_cli.py` green.
Notes:
  Split from Phase 1: this ticket owns the Python package itself (pyproject, src tree, tests). CI / pre-commit / README / .env.example are in CCR-002 because Phase 1 touches both source code and `.github/workflows/`, which the workflow rule says to split. Packages to install per plan §8 Phase 1: aiogram, fastapi, uvicorn[standard], httpx, pydantic, pydantic-settings, sqlalchemy[asyncio], aiosqlite, alembic, pyjwt, prompt_toolkit, structlog; dev: pytest, pytest-asyncio, ruff, mypy, pre-commit. Ruff: line length 100, target-version py312, all rules + ignore E501 in tests. Mypy: strict, exclude `alembic/versions`. `[project.scripts] ccr = "ccr.cli:main"`.

### Review log

---

## CCR-002: CI workflow, pre-commit, env template, README placeholder [todo]
Phase: 1
Feature: core
Files:
  - `.env.example` — full template per Section 7.
  - `.pre-commit-config.yaml` — ruff, ruff-format, mypy hooks.
  - `.github/workflows/ci.yml` — install uv, run `uv sync`, `ruff check`, `ruff format --check`, `mypy src`, `pytest`.
  - `README.md` — placeholder with project summary and "Run locally in 60 seconds" section reserved.
Out of scope:
  - Any business logic, DB, env loading.
Acceptance:
  - [ ] CI workflow file is valid YAML.
Depends on: CCR-001
Notes:
  Split from Phase 1 per the project-manager rule: phase touches both source code and `.github/workflows/`, so the sysops piece (CI + dev tooling + env template + README placeholder) is its own ticket. `.env.example` should match the env list in plan §7. `README.md` is a placeholder; the polished version comes in Phase 14.

### Review log

---

## CCR-003: Configuration, DB schema, Alembic migrations [todo]
Phase: 2
Feature: core
Files:
  - `src/ccr/config.py` — `Settings(BaseSettings)` with all env vars from Section 7, `model_config=SettingsConfigDict(env_file=".env", extra="ignore")`. Field validators: `JWT_SECRET` ≥ 32 chars, `WEB_PORT` 1–65535, `PROXY_PORT_ALLOWLIST` parsed to `set[int] | None`, `TOKEN_TTL_SECONDS` 60–86400.
  - `src/ccr/logging_setup.py` — `configure_logging(level: str)` using structlog with console renderer.
  - `src/ccr/db/engine.py` — `create_engine_from_settings(settings)`, `AsyncSessionMaker`, `get_session()` async context manager.
  - `src/ccr/db/models.py` — `Base = DeclarativeBase`, `PairedUser`, `PairingCode`, `Session` SQLAlchemy models matching Section 5 (UUIDs as `Uuid(as_uuid=True)`, partial unique index on `is_owner = true`).
  - `alembic.ini` — `sqlalchemy.url = sqlite+aiosqlite:///data/ccr.db`, `script_location = alembic`.
  - `alembic/env.py` — async-aware env that imports `Base.metadata` from `ccr.db.models`.
  - `alembic/versions/0001_initial.py` — creates `paired_users`, `pairing_codes`, `sessions`.
  - `src/ccr/cli.py` — implement `init-db` subcommand that runs `alembic upgrade head` programmatically, ensures `DATA_DIR` and `DATA_DIR/logs` exist.
  - `tests/test_config.py` — Settings loads from a temp `.env`; rejects short JWT_SECRET; parses port allowlist correctly.
  - `tests/test_db_models.py` — engine bound to in-memory SQLite, `Base.metadata.create_all` runs, can insert + query a `PairedUser`; partial unique on `is_owner` prevents two owners.
Out of scope:
  - Any auth or business logic; only schema and config.
Acceptance:
  - [ ] `cp .env.example .env && JWT_SECRET=$(openssl rand -hex 32) ... && python -m ccr init-db` exits 0 and creates `data/ccr.db`.
  - [ ] `sqlite3 data/ccr.db ".tables"` lists `alembic_version`, `paired_users`, `pairing_codes`, `sessions`.
  - [ ] `pytest tests/test_config.py tests/test_db_models.py` passes.
  - [ ] `alembic downgrade base && alembic upgrade head` round-trips without error.
  - [ ] A test asserts that inserting a second `PairedUser` with `is_owner=True` raises `IntegrityError`.
Depends on: CCR-001
Notes:
  No new packages — all deps land in CCR-001. Migration `0001_initial.py` is hand-written, not autogenerated, to keep it readable. `cli.main` should call `configure_logging` before dispatch. Use `Mapped[...]` syntax in models. Wires `init-db` subcommand into the CLI stub from CCR-001.

### Review log

---

## CCR-004: Pairing auth — storage, owner model, CLI subcommands [todo]
Phase: 3
Feature: auth
Files:
  - `src/ccr/auth/pairing.py`:
    - `async def create_code(db, tg_user_id, tg_username) -> PairingCode` (8-char base32, TTL from settings)
    - `async def list_pending(db) -> list[PairingCode]`
    - `async def approve(db, code: str) -> PairedUser` — first approval auto-promotes to owner; later approvals set `approved_by_tg_user_id` to current owner
    - `async def revoke(db, tg_user_id: int) -> None` — refuses if target is owner
    - `async def invite(db, tg_user_id, label) -> PairedUser` — owner-only, pre-approves
    - `async def list_paired(db) -> list[PairedUser]`
    - `async def get_owner(db) -> PairedUser | None`
  - `src/ccr/auth/allowlist.py`:
    - `async def is_paired(db, tg_user_id: int) -> bool`
    - `async def is_owner(db, tg_user_id: int) -> bool`
  - `src/ccr/cli.py` — implement one-shot subcommands `pair list`, `pair pending`, `pair approve <code>`, `pair revoke <tg_user_id>`, `pair invite <tg_user_id> [--label X]`. Pretty-print results. Each subcommand calls the same functions the console will call.
  - `tests/test_pairing.py` — full lifecycle: create code → first approval becomes owner → second code → second approval is non-owner → revoke non-owner OK → revoke owner refused → expired code rejected → reused code rejected → invite happy path.
Out of scope:
  - Bot integration (Phase 5/6); console REPL (Phase 4).
Acceptance:
  - [ ] `python -m ccr pair list` prints `(empty)` on a fresh DB.
  - [ ] After manually inserting a `pairing_codes` row, `python -m ccr pair approve <code>` prints `Approved Telegram user 123456789 (owner)`. A subsequent `pair list` shows the user with `is_owner=True`.
  - [ ] A second pairing code, when approved, prints `Approved Telegram user 987654321 (paired)` with no owner promotion.
  - [ ] `python -m ccr pair revoke <owner_id>` exits 1 with `Cannot revoke owner.`
  - [ ] `python -m ccr pair revoke <friend_id>` succeeds and `pair list` no longer shows that user as active.
  - [ ] `pytest tests/test_pairing.py` passes.
Depends on: CCR-003
Notes:
  Code generation: `secrets.token_hex(4).upper()` collision-retried up to 5 times. `approve` is a single transaction. `is_paired` / `is_owner` filter on `revoked_at IS NULL`. Owner-only enforcement for `invite` lives at the CLI/console layer; the pure function trusts its caller. Tests should support manual `now` injection for expiry/reuse cases.

### Review log

---

## CCR-005: Console REPL app [todo]
Phase: 4
Feature: console
Files:
  - `src/ccr/console/app.py` — prompt_toolkit `PromptSession` with:
    - Command parser (split on whitespace, dispatch to handlers).
    - Auto-completion for `pair approve <pending-code>` and `pair revoke <paired-id>`.
    - History file at `data/console_history`.
    - Handlers calling the same `auth.pairing.*` functions used by the CLI.
    - `status` command: pings `http://{WEB_HOST}:{WEB_PORT}/healthz`, prints DB path, paired count, pending codes count.
  - `src/ccr/cli.py` — wire `console` subcommand to `console.app.run()`.
  - `tests/test_console.py` — drive the REPL programmatically via prompt_toolkit's `create_pipe_input`. Assert: `pair list` outputs expected table; unknown command prints help; `pair approve <bad>` shows error; happy path approves a pre-seeded code.
Out of scope:
  - Permission UI for inline buttons via console; that stays in Telegram.
Acceptance:
  - [ ] `python -m ccr console` opens a `ccr>` prompt and accepts commands.
  - [ ] `pair list` prints a column-aligned table.
  - [ ] `help` lists all commands. `unknown_cmd` prints `Unknown command. Type 'help'.`
  - [ ] `python -m ccr console --once "pair list"` prints the table and exits 0.
  - [ ] `pytest tests/test_console.py` passes.
Depends on: CCR-004
Notes:
  Small command registry pattern (`COMMANDS: dict[str, Callable]`). Aligned-column table rendering with no extra dep. Auto-complete words refreshed before each prompt by querying the DB. `status` uses `httpx.get(public_url + "/healthz", timeout=2)`. `--once "<cmd>"` flag for non-interactive scripted use. Tests use `prompt_toolkit.input.create_pipe_input` + `DummyOutput`. The `status` command pings `/healthz` which doesn't exist yet (Phase 11) — handle the connection error gracefully and still print DB stats.

### Review log

---

## CCR-006: Bot scaffold, allowlist middleware, pairing flow, owner notification [todo]
Phase: 5
Feature: chat-bot
Files:
  - `src/ccr/bot/app.py` — `def build_dispatcher(settings, db_factory) -> Dispatcher`; `async def run_polling(...)`.
  - `src/ccr/bot/middlewares.py` — `class AllowlistMiddleware(BaseMiddleware)` injecting `is_paired_user` flag and `last_chat_id` updates; short-circuits non-`/start` updates from non-paired senders with `"Not paired. Send /start to request access."`
  - `src/ccr/bot/notify.py` — `async def notify_owner(bot, db, message: str)` and `async def broadcast_paired(bot, db, message: str, exclude: set[int] = ...)`.
  - `src/ccr/bot/handlers/pairing.py` — `/start` handler:
    - if paired → reply with `"Paired. Send a prompt to start, or /new for a fresh session."`
    - else if owner exists → call `pairing.create_code`, reply with `"Access requested. The owner has been notified."` and `notify_owner` with the approve hint
    - else → call `pairing.create_code`, reply with the code + Telegram user ID + the exact `pair approve` command (this is the bootstrap path for the very first owner)
  - `src/ccr/server.py` — start dispatcher polling task; expose `async serve(settings)`.
  - `src/ccr/cli.py` — implement `serve` subcommand calling `server.serve`.
  - `tests/test_bot_pairing.py` — using aiogram's mock infrastructure, assert:
    - bootstrap (no owner): `/start` from unknown user replies with the code visible
    - normal (owner exists): `/start` from new user replies with "owner notified" *and* `notify_owner` was called with the code
    - paired user: `/start` shows status
    - unpaired plain text: rejection string
Out of scope:
  - Session commands. Web server still off.
Acceptance:
  - [ ] `python -m ccr serve` starts polling and logs `Bot started, awaiting updates`.
  - [ ] `pytest tests/test_bot_pairing.py` passes, covering all four paths above.
  - [ ] A test asserts `notify_owner` is *not* called in the bootstrap path.
  - [ ] A test asserts unpaired plain text returns the fixed rejection string and never reaches a session handler.
Depends on: CCR-004
Notes:
  Plan lists Phase 4 as "helpful but not blocking" — only Phase 3 (CCR-004) is a hard dependency. `AllowlistMiddleware` registers on both `dp.message` and `dp.callback_query` and persists `last_chat_id` on every message from a paired user (used later by notify functions). Owner DMs go to `paired_users.last_chat_id` for the owner; if null (owner hasn't messaged the bot since pairing), log a warning and skip. `cli.py serve` runs `asyncio.run(server.serve(settings))` — initially polling-only; uvicorn comes in Phase 10/11.

### Review log

---

## CCR-007: Claude subprocess wrapper, event bus, JSONL logger [todo]
Phase: 7
Feature: claude-runtime
Files:
  - `src/ccr/events/bus.py` — `class EventBus` with `subscribe(topic) -> AsyncIterator[Event]` and `async publish(topic, payload)`. Uses `asyncio.Queue` per subscriber, drops oldest on slow consumer with a warning.
  - `src/ccr/claude/events.py` — Pydantic discriminated-union `ClaudeEvent = Annotated[Union[SystemInit, UserTurn, AssistantTurn, ResultEvent, PermissionRequest, UnknownEvent], Field(discriminator="type")]`. Includes inner `ContentBlock` union (text/thinking/tool_use/tool_result).
  - `src/ccr/claude/process.py` — `class ClaudeProcess`:
    - `async start(self) -> None` (spawns subprocess)
    - `async send_user_turn(self, content: str | list[ContentBlock]) -> None`
    - `async send_permission_response(self, request_id: str, choice: str) -> None`
    - `async events(self) -> AsyncIterator[ClaudeEvent]`
    - `async stop(self, grace: float) -> None`
  - `src/ccr/claude/log.py` — `class JsonlSessionLog` with `append(event)`, `read_from(seq) -> AsyncIterator[(seq, event)]`, `tail() -> AsyncIterator[(seq, event)]`. Sequence numbers based on file line count. `prune(retention_count, retention_days)` for startup cleanup.
  - `src/ccr/claude/manager.py` — `class SessionManager`:
    - holds at most one running `ClaudeProcess` plus its `Session` row, `JsonlSessionLog`, and consumer task
    - `async new_session(prompt: str | None, started_by: int | None) -> UUID`
    - `async send(prompt: str) -> None`
    - `async send_permission(session_id, request_id, choice)`
    - `async stop() -> None`
    - `async status() -> SessionStatus`
    - on each event: write to log + publish to `bus`
    - on subprocess exit: update `Session.status` accordingly, publish synthetic `error` event on crash
  - `tests/fakes/fake_claude.py` — small Python script reading JSONL from stdin and writing canned JSONL to stdout based on directives in env vars.
  - `tests/test_claude_events.py` — round-trip parses each known event variant from fixture JSONL.
  - `tests/test_session_manager.py` — replace subprocess with the fake; assert events persisted, bus receives them, status transitions correctly, `stop()` is idempotent, crash detected.
Out of scope:
  - Real Claude Code invocation in tests; bot or web integration.
Acceptance:
  - [ ] `pytest tests/test_claude_events.py tests/test_session_manager.py` passes.
  - [ ] A test exercises the fake subprocess and asserts: 5 events persisted to JSONL with sequence 0–4; `EventBus` subscriber receives the same 5 events; `status()` transitions `idle → running → completed`.
  - [ ] A test asserts that killing the fake subprocess mid-stream sets `Session.status = 'crashed'` and publishes a synthetic error event.
  - [ ] `python -c "from ccr.claude.events import ClaudeEvent; from pydantic import TypeAdapter; print(TypeAdapter(ClaudeEvent).json_schema())"` prints a non-empty schema.
Depends on: CCR-003
Notes:
  Per plan §8 ordering note, Phase 7 must land BEFORE Phase 6 — this ticket is numbered CCR-007 and ordered first in TICKETS.md to make that explicit. `EventBus` uses weakref-tracked subscriber queues. Stderr is buffered and surfaced as `UnknownEvent(type="stderr", raw={...})` only on subprocess crash. `JsonlSessionLog` is append-only with `asyncio.Lock` for serialization; tail uses an internal `asyncio.Event` notified by `append()`. Startup pruning keeps last `LOG_RETENTION_COUNT` files and deletes files older than `LOG_RETENTION_DAYS`. `SessionManager` enforces single running session globally.

### Review log

---

## CCR-008: Session lifecycle handlers, Telegram formatting, multi-user broadcast [todo]
Phase: 6
Feature: chat-bot
Files:
  - `src/ccr/bot/formatting.py` — `def event_to_messages(event: ClaudeEvent) -> list[OutboundMessage]`:
    - text/thinking → chunks of ≤ 3500 chars split at sentence/newline boundaries
    - tool_use → one-line summary `"🔧 {tool_name} {short_args}"` (truncate args to 200 chars); never include full tool input
    - tool_result → ignored unless it carries an error → `"❌ {tool_name}: {first 200 chars}"`
    - result event → `"✅ done · {duration_ms}ms · ${cost}"` or `"❌ failed: {subtype}"`
    - permission_request → handled in Phase 8, returns `[]` here
    - unknown → `[]`
  - `src/ccr/bot/handlers/session.py`:
    - `/new` → `manager.new_session(prompt=None, started_by=msg.from_user.id)`, reply `"Session <id8> started."`
    - `/stop` → `manager.stop()`, reply `"Session stopped."` (idempotent: `"No active session."` if none)
    - `/clear` → `/stop` then `/new`
    - `/who` → owner sees full list, friends see count + owner's @username
    - plain text → `manager.new_session(prompt=text, started_by=...)` if idle else `manager.send(text)`; reply `"Forwarded."`
  - `src/ccr/server.py` — start a single broadcast task that subscribes to `bus.subscribe("session.event")` and forwards each formatted message to *all* paired users with `last_chat_id IS NOT NULL`. Concurrency: per-chat `asyncio.Queue` to avoid head-of-line blocking; one sender task per chat.
  - `tests/test_bot_session.py` — using a fake `SessionManager` and mocked bot, assert `/new`, `/stop`, plain text behaviour.
  - `tests/test_formatting.py` — feed each event variant through `event_to_messages` and assert chunking + summarization rules; assert no message exceeds 4096 chars.
  - `tests/test_broadcast.py` — three paired users with `last_chat_id` set; publish an event; assert all three sender queues receive the formatted message.
Out of scope:
  - Permission inline buttons (next phase). Slash passthrough commands. `/view` / `/preview`.
Acceptance:
  - [ ] `pytest tests/test_bot_session.py tests/test_formatting.py tests/test_broadcast.py` passes.
  - [ ] Formatting test asserts a 10 000-char text event becomes ≥ 3 messages, none > 4096 chars, with no mid-sentence cuts when paragraph breaks exist.
  - [ ] Broadcast test asserts events fan out to all paired users with known chat IDs.
  - [ ] Manual smoke: `/new` then `"List the files in this project"` produces streamed output in Telegram with file names visible but no full file contents dumped.
  - [ ] `/stop` on idle session returns `"No active session."` and exits cleanly; running it twice in a row doesn't crash.
Depends on: CCR-006, CCR-007
Notes:
  Plan flags this with `⚠️ ordering note`: Phase 7 (CCR-007) must land before this. Inject `SessionManager` via aiogram workflow data (`dp["session_manager"] = mgr`). Chunking rule: prefer `\n\n` split, fall back to `\n`, fall back to `. `, then hard cut at 3500 chars; keep code blocks intact when small. `TELEGRAM_HARD_LIMIT = 4096`, `SAFE_CHUNK = 3500`. The broadcast task is wired in `server.serve` after Phase 7's `SessionManager`. Manual smoke checklist for the real `claude` binary belongs in README.

### Review log

---

## CCR-009: Permission inline-button handling [todo]
Phase: 8
Feature: chat-bot
Files:
  - `src/ccr/bot/keyboards.py` — `def permission_kb(session_id, request_id, options) -> InlineKeyboardMarkup` with one button per option; `callback_data=f"perm:{session_id}:{request_id}:{choice}"`.
  - `src/ccr/bot/formatting.py` — augment to return `(text, keyboard)` for `PermissionRequest`. Update return type to `OutboundMessage = (text, keyboard | None)`.
  - `src/ccr/bot/handlers/permission.py` — `@router.callback_query(F.data.startswith("perm:"))` handler:
    - paired check (middleware already enforces)
    - parse session_id, request_id, choice; validate choice against allowed set
    - check `session_id` matches running session; if stale, `cb.answer("Stale prompt", show_alert=True)`
    - call `manager.send_permission(session_id, request_id, choice)`
    - edit message: remove keyboard, append `"→ {choice} (by @{cb.from_user.username})"` so all paired users see who answered
  - `src/ccr/claude/manager.py` — add `pending_permissions: dict[str, asyncio.Event]` and gating logic so the broadcast task pauses delivery to Telegram between a `permission_request` and the matching choice. SSE stream still gets all events live.
  - `tests/test_bot_permission.py` — mocked bot + fake manager. Cases: emit `permission_request` → keyboard sent; simulate `callback_query` from a paired user → `send_permission_response` called with right args, message edited; tap from a non-paired user → 401-equivalent rejection; stale session_id → alert, no manager call.
Out of scope:
  - Per-session permission UI in the web viewer (post-MVP).
Acceptance:
  - [ ] `pytest tests/test_bot_permission.py` passes.
  - [ ] A test asserts feeding `permission_request{options=["approve","skip","abort"]}` produces one message with three inline buttons whose `callback_data` matches the spec.
  - [ ] A test asserts tapping `Approve` calls `send_permission_response(request_id, "approve")` exactly once and the keyboard is removed.
  - [ ] A test asserts the edited message includes the responder's username.
  - [ ] Buffering test: while a permission is pending, an injected `text` event is held; once responded, the buffered event is delivered before any later events.
Depends on: CCR-008, CCR-007
Notes:
  Refactor `event_to_messages` (from CCR-008) to return the new `OutboundMessage` tuple type; broadcast task in `server.py` updated to handle keyboard-bearing messages. Buttons get short labels (`Approve`, `Skip`, `Abort`) with original choice strings preserved in `callback_data`. Gating logic in `SessionManager`: when a `permission_request` is published, increment a counter; broadcast task checks `manager.is_telegram_paused(session_id)` and buffers if paused, drains when response arrives. SSE stream is NOT paused — it continues to deliver all events live.

### Review log

---

## CCR-010: Slash-command passthrough whitelist [todo]
Phase: 9
Feature: chat-bot
Files:
  - `src/ccr/bot/handlers/passthrough.py` — `WHITELIST = {"cost", "model", "compact"}`; `BLOCKED_INTERACTIVE = {"agents", "mcp", "init"}`. Handler matches commands not already claimed by other routers.
  - `src/ccr/claude/manager.py` — add `async send_slash(self, name: str, args: str) -> None` that prepends `"/" + name + " " + args` and submits as a normal user turn (Claude Code interprets it the same as if typed in TTY).
  - `tests/test_passthrough.py` — `/cost` calls `send_slash("cost", "")`; `/agents` is blocked with the documented message; unknown `/foo` returns the usage hint.
Out of scope:
  - Expanding the whitelist.
Acceptance:
  - [ ] `pytest tests/test_passthrough.py` passes.
  - [ ] `/cost` produces a Telegram-side cost summary in the manual smoke test.
  - [ ] `/agents` produces: `"Interactive command — run /agents in your local Claude Code terminal."`
  - [ ] `/totallyunknown` returns: `"Unknown command. Whitelisted: /new /stop /clear /view /last /preview /cost /model /compact /who."`
Depends on: CCR-008
Notes:
  Three branches in the handler: whitelist (forward via `send_slash`), blocked-interactive (canned reply), unknown (usage hint). The unknown-command response lists slashes from later phases (`/view`, `/last`, `/preview`) — those land in Phase 13; the message text is fixed now to keep UX stable.

### Review log

---

## CCR-011: JWT signed-URL token module [todo]
Phase: 10
Feature: auth
Files:
  - `src/ccr/auth/tokens.py`:
    - `class TokenKind(StrEnum): VIEWER = "viewer"; PREVIEW = "preview"`
    - `def mint(kind: TokenKind, tg_user_id: int, payload: dict, settings: Settings) -> str`
    - `def verify(token: str, settings: Settings) -> VerifiedToken` (raises `TokenError`)
    - `def verify_kind(token, settings, expected: TokenKind) -> VerifiedToken`
    - Claims: `sub=tg_user_id`, `kind`, `payload`, `iat`, `exp`, `jti`
  - `tests/test_tokens.py` — mint then verify; expired token rejected (TTL = 1800 default); tampered signature rejected; wrong kind rejected.
Out of scope:
  - HTTP layer (next phase).
Acceptance:
  - [ ] `pytest tests/test_tokens.py` passes.
  - [ ] A test asserts a token minted at T=0 with TTL=1800 fails verification at T=1801.
  - [ ] A token with `kind=viewer` fails `verify_kind(..., PREVIEW)` with a clear error.
  - [ ] A test asserts the JWT payload includes `sub`, `kind`, `iat`, `exp`, `jti`.
Depends on: CCR-003
Notes:
  HS256 signing with `JWT_SECRET` from settings (≥ 32 chars enforced in CCR-003). Payload is encoded as inner JSON (e.g. `{"port": 3000}` for preview). Tests use manual `now` injection (`leeway=0`). No DB writes — tokens are stateless. This module is consumed by Phase 11 (web auth handoff) and Phase 13 (`/view`/`/last`/`/preview` URL minting in the bot).

### Review log

---

## CCR-012: Web server, auth handoff, sessions REST + SSE [todo]
Phase: 11
Feature: web-viewer
Files:
  - `src/ccr/web/app.py` — `def create_app(settings, manager, bus, db_factory) -> FastAPI`; CORS allow `PUBLIC_URL` only.
  - `src/ccr/web/auth.py`:
    - `GET /auth?token=<jwt>&next=<path>` → verify, set cookie `ccr_session` (30 min TTL, HttpOnly, Secure, SameSite=Lax), 302 to `next` (validate `next` starts with `/viewer` or `/app/`).
    - `class CookieAuthDep` FastAPI dependency that extracts cookie and calls `verify_kind(...)` per route requirement.
  - `src/ccr/web/sessions.py`:
    - `GET /api/sessions` → list `Session` rows (ordered `started_at desc`, limit 50).
    - `GET /api/sessions/{id}/events?from=<seq>` → SSE: replay JSONL from `from` then tail via `bus.subscribe("session.event")` filtered to `id`; close on session `completed/stopped/crashed` after the final `result` event.
    - `GET /api/sessions/{id}/log` → stream the JSONL file as `application/x-ndjson`.
    - `GET /healthz` → `{"ok": true, "session_status": ...}`.
  - `src/ccr/server.py` — `serve()` now starts both the dispatcher polling task *and* `uvicorn.Server(Config(app, host, port, lifespan="on"))` as concurrent tasks; cancellation cascades.
  - `tests/test_web_auth.py` — using `httpx.AsyncClient(app=app)`:
    - `GET /viewer` without cookie → 401 (or redirect-to-/auth)
    - `GET /auth?token=<valid>&next=/viewer` → 302 + Set-Cookie
    - subsequent `GET /viewer` with cookie → 200
    - expired token → 401
    - `next=/evil/path` → 400
  - `tests/test_sse_stream.py` — replay-then-tail: pre-seed JSONL with 3 events, subscribe with `from=0`, assert client receives 3 events, then publish 1 more to bus, assert client receives 4th.
Out of scope:
  - Frontend HTML; reverse proxy.
Acceptance:
  - [ ] `pytest tests/test_web_auth.py tests/test_sse_stream.py` passes.
  - [ ] `python -m ccr serve` boots both bot and uvicorn; `curl http://127.0.0.1:8765/healthz` returns `{"ok": true, ...}`.
  - [ ] `curl -i "http://127.0.0.1:8765/api/sessions"` without cookie returns 401.
  - [ ] After minting a token via a small test script, `curl -L -c /tmp/c -b /tmp/c "http://127.0.0.1:8765/auth?token=...&next=/api/sessions"` returns the JSON list.
  - [ ] SSE smoke: `curl -N -b /tmp/c "http://127.0.0.1:8765/api/sessions/<id>/events"` receives `data:` lines while a session runs.
Depends on: CCR-007, CCR-008, CCR-011
Notes:
  Plan lists Phase 11 deps as Phases 7 + 10, but `serve()` orchestration here adds `web_task` to the existing `gather(bot_task, broadcast_task)` from Phase 6 — so CCR-008 is also a hard dep. Most files land under `src/ccr/web/` (web-developer territory); `src/ccr/server.py` is python-developer territory but the change is small (~10 lines wiring uvicorn into the existing gather). Orchestrator should route to web-developer with awareness that the server.py edit is included; not splitting per the "don't over-slice" rule (the trigger for splitting is `src/ccr/{bot,auth,claude,console,db,events}/` AND `src/ccr/web/`, and server.py isn't in those subpackages). SSE implemented manually as `StreamingResponse`; heartbeat comment every 15 s. `next` validation whitelist prefixes only.

### Review log

---

## CCR-013: Multi-tab viewer frontend (xterm.js via CDN) [todo]
Phase: 12
Feature: web-viewer
Files:
  - `src/ccr/web/static/viewer.html` — bare scaffold with a tab strip container, terminal container, and:
    ```html
    <link rel="stylesheet"
          href="https://cdn.jsdelivr.net/npm/xterm@5.5.0/css/xterm.css"
          integrity="sha384-..." crossorigin="anonymous">
    <script src="https://cdn.jsdelivr.net/npm/xterm@5.5.0/lib/xterm.js"
            integrity="sha384-..." crossorigin="anonymous"></script>
    <script type="module" src="/static/viewer.js"></script>
    ```
    (Real SRI hashes generated during this phase via `curl ... | openssl dgst -sha384 -binary | openssl base64 -A`.)
  - `src/ccr/web/static/viewer.css` — mobile-first horizontal scrolling tab strip; xterm container fills below.
  - `src/ccr/web/static/viewer.js`:
    - On load: `GET /api/sessions` → render tabs.
    - Per tab, lazy-init `Terminal` from xterm.js when activated; open `EventSource('/api/sessions/{id}/events?from=' + lastSeq)` and dispatch by event type:
      - text/thinking → `term.write(payload + "\r\n")`
      - tool_use → coloured one-line summary
      - tool_result → coloured success/error summary
      - permission_request → `[permission requested: {tool} - awaiting tap in Telegram]`
      - result → final summary; close EventSource
    - Status indicator: setInterval every 5 s recomputing from `lastEventTs`.
    - Tab close button (only for completed/stopped/crashed tabs) — local-only dismiss.
    - Reconnect: on EventSource error, retry after 2 s with exponential backoff up to 30 s; pass last received `seq` as `?from=`.
  - `src/ccr/web/viewer.py` — `GET /viewer` returns `viewer.html` with cookie auth required; static files served via `FastAPI.mount("/static", StaticFiles(directory=...))`.
  - `tests/test_viewer.py` — `GET /viewer` without cookie → 401; with cookie → 200 and `Content-Type: text/html`. Assert SRI hashes are present in the served HTML.
Out of scope:
  - Per-subagent tabs; writing back to sessions; theming; offline use.
Acceptance:
  - [ ] `pytest tests/test_viewer.py` passes.
  - [ ] Manual: open `/viewer` after `/view` link from Telegram → see at least one tab; run `/new "say hello"` → live text appears in the active tab within 2 s; tab indicator goes from green → grey on `result`.
  - [ ] Manual offline-test: with the dev machine's internet disabled, viewer page fails to load (expected with CDN choice). Documented as a known trade-off in README.
  - [ ] Disconnect (kill server) for 5 s, restart → viewer reconnects and resumes from last seq without showing duplicates.
  - [ ] Resize: on a 360 px viewport, tab strip scrolls horizontally; terminal fills remaining height; no horizontal scroll on the body.
  - [ ] Browser dev tools confirm xterm.js loaded with `integrity` matching; tampering the SRI hash makes the page fail to load (manual check).
Depends on: CCR-012
Notes:
  All files in `src/ccr/web/` — web-developer territory. Generate SRI hashes for `xterm@5.5.0` JS and CSS at ticket-completion time and commit them in `viewer.html`; document the regeneration command in a comment. Single ES module, no bundler. Dark bg + ANSI-256. Reconnect with exponential backoff to 30 s and `?from=` resume on `EventSource` errors. Status indicator colours via CSS classes driven by `data-state` attribute. Manual smoke checklist for mobile + desktop belongs in README. The "/view link from Telegram" referenced in acceptance is implemented in CCR-014 (Phase 13); team lead should test that step after CCR-014 lands.

### Review log

---

## CCR-014: Bot handlers `/view`, `/last`, `/preview` [todo]
Phase: 13
Feature: chat-bot
Files:
  - `src/ccr/bot/handlers/view.py`:
    - `/view` → mint VIEWER token; reply `f"{PUBLIC_URL}/auth?token={t}&next=/viewer"` with a "valid 30 min" note.
    - `/last` → look up most recent session id; mint VIEWER token; reply with `next=/viewer?session={id}`.
    - `/preview <port>` → validate `1 ≤ port ≤ 65535` and (if `PROXY_PORT_ALLOWLIST` set) port is in it; mint PREVIEW token with `payload={"port": port}`; reply with `next=/app/{port}/`.
Out of scope:
  - WebSocket proxying.
Acceptance:
  - [ ] `/preview 999999` returns `"Port out of range"`.
  - [ ] With `PROXY_PORT_ALLOWLIST=3000`, `/preview 9000` returns `"Port 9000 not in PROXY_PORT_ALLOWLIST."`.
  - [ ] `/view` link works with no active session and shows the session list (empty state).
Depends on: CCR-011, CCR-012
Notes:
  Split from Phase 13: this ticket owns the bot-side handlers (python-developer territory `src/ccr/bot/handlers/`). The proxy half is CCR-015. Split fires because Phase 13 touches both `src/ccr/bot/` and `src/ccr/web/`. URL minting helper: `def build_url(settings, kind, payload, next_path) -> str`. The `/view` empty-state acceptance assumes `/viewer` exists (CCR-013) — depend on CCR-012 for the `/auth` → cookie flow that the URL relies on; CCR-013 (viewer HTML) makes the empty state render but is not strictly required for the bot reply itself.

### Review log

---

## CCR-015: Localhost reverse proxy [todo]
Phase: 13
Feature: web-viewer
Files:
  - `src/ccr/web/proxy.py`:
    - `@app.api_route("/app/{port:int}/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","HEAD","OPTIONS"])`
    - Cookie auth required; verify token kind == PREVIEW and `payload.port == port`.
    - Use a singleton `httpx.AsyncClient(timeout=30, follow_redirects=False)`. Stream both directions.
    - Strip `Cookie` and `Authorization` request headers; rewrite `Host` to `127.0.0.1:{port}`; pass through `X-Forwarded-{For,Proto,Host,Prefix}`.
    - Return upstream `Content-Type` and body verbatim. Reject `Upgrade: websocket` with 502 + clear error body.
  - `tests/test_proxy.py` — spin a tiny test server on a random port; assert `/app/<port>/foo` returns its body, with cookie auth, and 401 without; 403 if port not allowlisted; 403 if cookie token's payload.port doesn't match URL port.
Out of scope:
  - WebSocket proxying.
Acceptance:
  - [ ] `pytest tests/test_proxy.py` passes.
  - [ ] Manual: run `python -m http.server 9000` in another terminal; `/preview 9000` → tap link → see the directory listing.
Depends on: CCR-012, CCR-014
Notes:
  Split from Phase 13: this ticket owns the proxy under `src/ccr/web/` (web-developer). `CCR-014` is the bot-handler half. The manual `/preview 9000` acceptance straddles both — listed here because the proxy is what makes it work end-to-end (the bot handler is mocked in CCR-014 tests). Streaming via `StreamingResponse` over `client.stream()`. Hop-by-hop header stripping per RFC 7230 § 6.1. Optional `PROXY_PORT_ALLOWLIST` enforcement (the bot side rejects upfront in CCR-014; the proxy still enforces defensively). Tests use a pytest-managed upstream fixture.

### Review log

---

## CCR-016: Doctor subcommand and serve preflight checks [todo]
Phase: 14
Feature: bootstrap
Files:
  - `src/ccr/server.py` — preflight checks: Claude CLI present, DB up to date, `PUBLIC_URL` reachable best-effort warning (`httpx.get(public_url, timeout=2)` and log only).
  - `src/ccr/cli.py` — `doctor` subcommand printing all preflight check results.
  - `tests/test_doctor.py` — doctor command runs and exits 0 on healthy environment, 1 on broken.
Out of scope:
  - docker-compose; cloud deployment; CI release pipeline.
Acceptance:
  - [ ] `python -m ccr doctor` prints `OK` for: Python version, claude binary, DB ready, JWT_SECRET set, PUBLIC_URL reachable, xterm.js CDN reachable.
Depends on: CCR-007, CCR-012
Notes:
  Split from Phase 14: this ticket owns the python-developer pieces (`src/ccr/server.py` preflight, `src/ccr/cli.py` doctor, `tests/test_doctor.py`). The sysops half (`install.sh`, `.gitmodules`, `README.md`) is CCR-017. Split fires per the rule "phase touches `install.sh` … and source code". `serve()` startup uses the same checks as `doctor` but only logs warnings rather than exiting. Plan lists Phase 14 deps as "all previous phases" — encoded here as CCR-007 (claude binary check, SessionManager) + CCR-012 (server.py orchestration, healthz endpoint). DB and JWT_SECRET checks rely on CCR-003.

### Review log

---

## CCR-017: install.sh, submodule, README, final repo polish [todo]
Phase: 14
Feature: bootstrap
Files:
  - `install.sh`:
    - Check Python ≥ 3.12, `uv` installed (offer one-line installer command), `claude` on PATH.
    - `git submodule update --init --recursive`.
    - `uv sync`.
    - Generate `JWT_SECRET=$(openssl rand -hex 32)` and write to `.env` if absent (copying from `.env.example` first).
    - `mkdir -p data/logs`.
    - `python -m ccr init-db`.
    - Print next steps (Telegram bot token, tunnel setup, console).
  - `.gitmodules` — single submodule `templates/ → <dev-stack-agents repo URL>` on `main`.
  - `README.md`:
    - Project summary
    - "Run locally in 60 seconds" — exact commands
    - Console walkthrough (how to invite a friend)
    - Tunnel setup (Tailscale Funnel; alternatives noted)
    - Bot command reference + console command reference
    - Troubleshooting
    - Security model section: trust boundaries, what 30-min tokens protect against, why CDN xterm.js
    - License (confirm with project owner)
Out of scope:
  - docker-compose; cloud deployment; CI release pipeline.
Acceptance:
  - [ ] On a clean VM with Python 3.12 + uv + Claude Code installed: `git clone --recurse-submodules ... && cd ... && ./install.sh` completes without errors in under 2 min.
  - [ ] `pytest` (full suite) passes.
  - [ ] `mypy src` passes.
  - [ ] `ruff check src tests` passes.
  - [ ] README "60 seconds" section, when followed verbatim, results in a working bot reachable via Telegram with the bootstrap user paired.
  - [ ] README "invite a friend" walkthrough, when followed, results in a second paired user receiving session output in their chat.
Depends on: CCR-013, CCR-014, CCR-015, CCR-016
Notes:
  Split from Phase 14: this ticket owns the sysops pieces (`install.sh`, `.gitmodules`, `README.md`) plus the final repo-wide test/lint/type pass. The python-developer doctor work is CCR-016. `install.sh` uses `set -euo pipefail` and idempotent steps. The "60 seconds" README walkthrough must use these exact commands per plan §8 Phase 14 task 4: `git clone --recurse-submodules`, `cd claude-code-remote`, `./install.sh`, edit `.env` (TELEGRAM_BOT_TOKEN, PUBLIC_URL), `python -m ccr serve`, send `/start` in Telegram, then `python -m ccr pair approve <code>` (or `python -m ccr console`). Include the "invite a friend" walkthrough verbatim from plan task 5. README author should confirm license with project owner before merging.

### Review log
