# Brief: chat-bot

## Purpose
The aiogram-based Telegram bot. This is the user's primary control surface: paired users send prompts and slash commands; the bot forwards them into the running Claude session and broadcasts the resulting events back as formatted HTML messages with inline keyboards for permission gates and AskUserQuestion prompts. Owns the `/start` pairing flow, the allowlist middleware, the per-chat broadcast loop, the typing-indicator keepalive, and the slash-command handlers (built-in + passthrough whitelist).

## Key invariants
- Every Telegram update passes through `AllowlistMiddleware`. Unpaired senders are short-circuited; only `/start` is allowed through. The middleware persists `last_chat_id` on paired senders so `notify_owner` and `broadcast_paired` can reach them later.
- The broadcast loop pauses **only Telegram fan-out** while a permission request or AskUserQuestion is pending; SSE keeps streaming. When the gate reopens (button tap or `/answer`), the buffered events drain in order.
- Allowlist is keyed on `tg_user_id`. `@username` is never load-bearing — it can change.
- `/start` has three branches: already-paired, bootstrap (no owner exists yet — auto-promote on approval), normal (paired, code-issuing).
- `cb_permission` and AskUserQuestion handlers validate every callback / answer against `_pending_options` / pending-id whitelists. Forged choices and stale ids are rejected with canned messages; concurrent taps are absorbed via `contextlib.suppress(TelegramBadRequest)`.
- All user-controlled strings (usernames, tool inputs, session ids, errors) go through `html.escape()` before reaching `parse_mode="HTML"`.
- `chunk_text` caps every outbound message body at the Telegram limit; `event_to_messages()` returns a list because some events don't fit in one message.
- `/clear` broadcasts a divider (`"— — — new session — — —"`) to all paired chats *only* when `prior_status != IDLE` — idle clears stay quiet.
- `/sessions` body is hard-capped at 3500 chars via `chunk_text`; empty DB returns the stable `"(no sessions)"` string.
- Slash-command passthrough is a tight whitelist (`{"model", "compact"}`); known-blocked interactives (`{"mcp", "init"}`) return a canned local-terminal redirect. Unknown commands return the pinned usage hint.
- `/cost`, `/usage`, `/agents`, `/skills`, `/config` are local renders, NOT passthroughs to the upstream `claude` CLI.
- `cfg:*` callbacks use the calling `tg_user_id` (`cb.from_user.id`) as the DB lookup key — never trust the callback payload for the update target. Callback data carries only an integer index into a server-controlled list.
- Timezone validation MUST use stdlib `zoneinfo.ZoneInfo(name)` and treat `ZoneInfoNotFoundError` as a user error (no DB write).
- `paired_users.timezone` is nullable; NULL means UTC at render time. Do not hard-code a server default in the migration or the model.
- Every datetime rendered in `src/ccr/bot/` goes through `ccr.utils.format_user_datetime`. Inline `strftime` in `src/ccr/bot/` is forbidden — enforced by the grep canary `grep -rnE "strftime\(" src/ccr/bot/`.
- `format_user_datetime` never raises: an unresolvable `user.timezone` logs a structlog warning and falls back to UTC.
- Naive `datetime` input to the helper is interpreted as UTC (because SQLite drops `tzinfo` on round-trip even with `DateTime(timezone=True)`).
- `Session.name` auto-fill is gated by a Python-side `row.name is not None` guard after SELECT; equivalent to WHERE-NULL under the single-session invariant (at most one session runs at a time). A manual `/rename` always wins. The `_auto_fill_session_name` helper enforces this; `_db_insert_session` defers to it rather than setting `name` on the initial INSERT.
- `/rename` overwrites `Session.name` unconditionally — manual rename always wins, even over an existing manual name.
- `/sessions` rows where `Session.name IS NULL` render the literal `(unnamed)` placeholder (not an empty cell).
- `/sessions` rows render `by @<username>` from `paired_users.tg_username`; falling back to `by <tg_user_id>` when the join misses or the username is NULL. The `paired_users.first_name` column is explicitly NOT consulted.
- `Session.name` is capped at 40 characters whether it arrives via auto-fill or `/rename`.
- User-facing hint strings sent under the bot's HTML parse mode must HTML-entity-escape any literal `<` or `>` (CCR-037 set the precedent for `_RENAME_USAGE_HINT`; CCR-038 is expected to extend this audit to all other handlers).

## Public surface
### Entry point
- `src/ccr/bot/app.py::build_dispatcher(settings, db_factory, session_manager=None) -> tuple[Bot, Dispatcher]` — registers `AllowlistMiddleware`, stashes `db_factory`, `session_manager`, `settings` in workflow data; includes pairing, session, permission, ask-user-question, config, and passthrough routers (passthrough must be last).
- `src/ccr/bot/app.py::run_polling` — logs `"Bot started, awaiting updates"` then enters `dp.start_polling(bot)`.

### Middleware + notification
- `src/ccr/bot/middlewares.py::AllowlistMiddleware` — extracts sender, checks `auth.allowlist.is_paired`, injects `is_paired_user`, persists `last_chat_id`, short-circuits non-`/start` traffic from unpaired senders.
- `src/ccr/bot/notify.py::notify_owner` — sends to the owner's `last_chat_id`; warns if no owner / null `last_chat_id`.
- `src/ccr/bot/notify.py::broadcast_paired` — fans out to all paired users with non-null `last_chat_id`, skips an `exclude` set, swallows per-recipient `TelegramAPIError`.

### Handlers (`src/ccr/bot/handlers/`)
- `pairing.py` — `/start` (paired / bootstrap / normal branches; HTML username escape).
- `session.py` — `/new`, `/stop`, `/clear`, `/who`, `/pid`, `/sessions`, `/continue`, `/rename`, plain-text passthrough. `/clear` divider gated on `prior_status != IDLE`. `/sessions` lists 20 most-recent rows. `/continue` accepts an optional 8-hex prefix; rejects bad format without manager call. plain-text filter is `F.text & ~F.text.startswith("/")` — slash commands fall through to passthrough. `_format_session_row(row, user, usernames)` renders each session as `<claude_session_id> · <status> · <name> · HH:MM - D Mon · by @<username>` via `format_user_datetime(..., "short")` with documented NULL fallbacks (UUID prefix for legacy rows, `(unnamed)` for NULL name, `by <tg_user_id>` when username is missing).
- `session.py::cmd_rename` — `/rename <8-hex-prefix> <name>`; overwrites `Session.name` unconditionally (manual rename always wins). Returns stable usage / unknown-prefix / ambiguous-prefix replies; truncates to 40 chars; HTML-escapes the rendered name.
- `session.py::_format_started_by` — new helper for the trailing `by …` slot; renders `by @<username>` from `paired_users.tg_username` when available, else `by <tg_user_id>`. Explicitly does NOT consult `first_name`.
- `permission.py::cb_permission` — callback `perm:{session_id}:{request_id}:{choice}`; validates choice against `_pending_options` frozenset; rejects forged / stale / concurrent / malformed; edits message with `→ {choice} (by @{username})`.
- `ask_user_question.py` — three reply paths (button tap callback, `/answer <id8> <text>` command, single-outstanding plain-text feed). `_ID8_RE = ^[0-9a-zA-Z_-]{8}$` (broadened for real-world prefixes like `toulu_…`). Stale / unknown ids rejected with canned message. Validates `tool_use_id`, calls `session_manager.send_tool_result`.
- `passthrough.py` — three-branch dispatch: `WHITELIST = {"model", "compact"}` forwarded via `manager.send_slash`; `BLOCKED_INTERACTIVE = {"mcp", "init"}` returns canned redirect; unknown commands return usage hint. Dedicated branches for `/agents` (Running + Library), `/skills`, `/cost`, `/usage`.
- `config.py` — `cfg:*` router; `/config` opens an inline-keyboard menu (Timezone + Close); `/config tz <IANA name>` is the free-text fallback that validates via `zoneinfo.ZoneInfo` and persists to `paired_users.timezone` for the caller.
- `config.py::cmd_config` — `/config` handler; opens menu, or honours `/config tz <IANA name>` free-text fallback.
- `config.py::cb_open_tz_picker` — `cfg:tz` callback; renders the curated picker (13 zones).
- `config.py::cb_pick_tz` — `cfg:tz:<idx>` callback; persists `_CURATED_ZONES[idx]` for the caller.
- `config.py::cb_close` — `cfg:close` callback; edits to "Menu closed." and drops `reply_markup`.

### Datetime helper
- `ccr.utils.format_user_datetime(dt, user, mode) -> str` — single source of truth for rendering datetimes in bot replies; modes `"full"` (`HH:MM - DD/MM/YYYY`), `"short"` (`HH:MM - D Mon`, English month abbreviation, no leading zero on day), `"time"` (`HH:MM`); applies `user.timezone` (defensive UTC fallback on `None` / unresolvable zone).

### DB schema additions (chat-bot)
- `src/ccr/db/models.py::PairedUser.timezone` — new nullable IANA-zone column; NULL means UTC at render time (consumer is CCR-035).
- `alembic/versions/0002_add_paired_users_timezone.py` — additive migration; reversible.
- `src/ccr/db/models.py::Session.name` — new nullable `Text` column; NULL rows render `(unnamed)` in `/sessions`.
- `alembic/versions/0004_add_sessions_name.py` — additive migration adding nullable `name TEXT` to `sessions`; reversible (downgrade drops column).

### Session name helpers (`src/ccr/claude/manager.py`)
- `SessionManager._auto_fill_session_name(session_id, prompt)` — new UPDATE-WHERE-NULL helper; called from `_db_insert_session` after row insert when a prompt is provided. Fires only when `name IS NULL` so manual renames are never stomped.
- `_truncate_session_name(text)` — module-level helper capping any session-name string at 40 chars with U+2026 ellipsis on hard-cut; collapses internal whitespace first.

### Keyboards (`src/ccr/bot/keyboards.py`)
- `permission_kb(session_id, request_id, options) -> InlineKeyboardMarkup` — one button per option using `_LABELS` (`{"allow": "Allow", "deny": "Deny", ...}`); unknown options fall back to `opt.capitalize()`. Callback data: `perm:{session_id}:{request_id}:{choice}`.
- `ask_user_question_kb(tool_use_id, options) -> InlineKeyboardMarkup` — option-index callbacks under `ask:` prefix (kept under Telegram's 64-byte cap).

### Formatting (`src/ccr/bot/formatting.py`)
- `chunk_text(text)` — splits to Telegram's per-message cap.
- `event_to_messages(event: ClaudeEvent) -> list[OutboundMessage]`, where `OutboundMessage = tuple[str, InlineKeyboardMarkup | _PendingKeyboard | None]`.
- `_PendingKeyboard.kind` ∈ `{"permission", "ask_user_question"}` — sentinel resolved by `server._materialise_keyboards` (the formatter cannot see `session_id`).
- `_AUQ_SUPPRESSED_TOOL_NAMES = frozenset({"AskUserQuestion"})` — defensive guard returning `[]` for suppressed tool names.
- Result line format: `✅ done · {s}s · {N}k tokens` (post-CCR-018).

### Typing
- `src/ccr/bot/typing.py::TypingKeepalive` — per-chat task sending `send_chat_action("typing")` every 4s while a session is producing events; swallows `TelegramAPIError`; `start` / `cancel` / `wait_closed` lifecycle.

### Server glue (cross-listed from claude-runtime, but lives in chat-bot land)
- `src/ccr/server.py::serve(settings)` — runs bot polling, `_broadcast_loop`, `_typing_loop` concurrently. `_ChatSender` per-chat queue; `_broadcast_loop` materialises `_PendingKeyboard` sentinels via `_materialise_keyboards` (dispatching on `kind`); special-cases permission requests (flush buffer + send immediately); buffers non-permission events while a gate is paused; drains buffer on gate reopen. Calls `manager.shutdown()` in `finally`.

### CLI glue
- `src/ccr/cli.py::_cmd_serve` — wires `serve` subcommand to `asyncio.run(server.serve(Settings()))`.

### Bot commands available today
- `/start`, `/new`, `/stop`, `/clear`, `/who`, `/pid`, `/sessions`, `/continue`, `/rename <8-hex-prefix> <name>`, `/answer <id8> <text>`, `/agents` (live Running + `.claude/agents/*.md` Library), `/skills` (from `system/init`), `/cost` (rich HTML), `/usage` (rate-limit snapshot), `/config` (per-user preferences — timezone picker + free-text fallback), `/model`, `/compact` (passthroughs), and the canned-redirect for `/mcp`, `/init`.

## Subtleties / gotchas
- **`/agents` reads from `.claude/agents/*.md` on disk** — that path is a Claude Code convention and is correct; do *not* repoint it to `docs/` or `plans/`. Tests in `test_bot_passthrough.py` populate `tmp_path/.claude/agents/` to verify.
- **HTML escape every user-controlled string.** Telegram HTML mode is forgiving but tests pin escape coverage. The pre-existing F1 advisory in `pairing.py` (unescaped username in HTML) was deferred at the time; revisit before adding more HTML messages there.
- **Pre-existing F1 advisory in `AllowlistMiddleware`**: an unpaired callback-query tap does not call `cb.answer()`, leaving Telegram's spinner hanging. Tracked as a separate ticket; not regressed by current handlers.
- **`_PendingKeyboard` is a sentinel because the formatter is pure** and cannot resolve `session_id` itself. Resolution happens in `server._materialise_keyboards` where the manager is available.
- **AskUserQuestion source-suppresses at MCP** (claude-runtime owns that). The formatter's `_AUQ_SUPPRESSED_TOOL_NAMES` is *defensive only*. Do not rely on the formatter as the primary suppression — schema drift would leak the envelope.
- **Slash-command filter narrowing.** `handle_text` excludes slash commands so they fall through to `passthrough.py`. If you add a new slash-command handler, register it before `passthrough_router` (which is included last in `app.py`).
- **`/clear` divider** is gated on `prior_status != IDLE` and the read must happen before `manager.stop()` (status flips after stop).
- **`/continue` argument validation** uses `_HEX8_RE` on the raw argv — invalid format must reject without invoking the manager (regression risk).
- **`cfg:` callback prefix is namespaced** separately from `perm:` and `auq:` — adding new callback prefixes elsewhere must pick a fresh namespace to avoid collisions on aiogram's `F.data.startswith(...)` filters.
- **`cb_open_tz_picker` matches `F.data == "cfg:tz"` (exact equality)** while `cb_pick_tz` matches `F.data.startswith("cfg:tz:")` (trailing colon) — both coexist because aiogram dispatches by registration order and the filters are mutually exclusive on payload shape.
- **Even curated zones go through `zoneinfo.ZoneInfo` server-side** before persistence — defence-in-depth against a future typo in `_CURATED_ZONES` silently writing a bad zone.
- **`/cost` cost line is omitted when `total_cost_usd == 0`** — that signals a subscription user whose cost isn't tracked. Don't emit `$0.00`.
- **`broadcast_paired` swallows per-recipient `TelegramAPIError`.** Don't add a global try/except that hides the per-recipient warning logs.
- **TypingKeepalive cancel is idempotent.** Calling `cancel` on an already-cancelled task is fine; the broadcast loop relies on this.
- **SQLite roundtrip strips `tzinfo` from `DateTime(timezone=True)` columns.** Do not assume `Session.started_at.tzinfo is not None` when reading back from the DB. The `format_user_datetime` helper handles this defensively (treats naive input as UTC), but other code paths must remain aware.
- **`_format_resets_at` in `passthrough.py` still passes `user=None` to `format_user_datetime`** (UTC fallback). Threading the calling `PairedUser` through that path is owned by CCR-039.
- **Telegram's HTML parse mode treats `<word>` as a tag start.** Any user-facing string sent with `parse_mode="HTML"` containing literal `<` or `>` will raise `TelegramBadRequest`. Use `&lt;`/`&gt;` for static placeholder text or `html.escape()` for dynamic content.
- **`cmd_answer`'s usage-hint send is wrapped in a narrow `try/except TelegramBadRequest`** (with structlog warning) so a future regression in any static `_USAGE_HINT`-shaped string cannot crash the dispatcher silently. The catch is intentionally narrow — broader exceptions still propagate.
- **The auto-fill cap (40 chars) is duplicated** as `_SESSION_NAME_MAX_LEN` in both `src/ccr/claude/manager.py` and `src/ccr/bot/handlers/session.py`; the bot module does not import manager-internal constants. If the cap changes, both sites must update.
- **Auto-fill only fires when `new_session(prompt=...)` is called with a non-empty prompt.** Plain-text input via `manager.send` after a no-arg `/new` does NOT seed the name; that session shows `(unnamed)` until `/rename`.
- **`_auto_fill_session_name` runs synchronously inside `_db_insert_session`** (extra DB round-trip on session start) — chosen over fire-and-forget so `/sessions` issued immediately after start sees the seeded name. Failures log and degrade to `(unnamed)`.
- **`cmd_rename` loads ALL session rows for prefix matching** (mirrors `_db_lookup_resumable_claude_session_id`). Bounded by the on-disk session count; revisit if that ever grows.

## Cross-feature relations
- depends on: auth (allowlist + pairing), core (Settings, DB models, async engine), claude-runtime (every command speaks to `SessionManager`; the broadcast loop materialises `ClaudeEvent` + `McpPermissionRequest`).
- used by: nothing (this is the user-visible surface; web-viewer is a separate read-only surface).

## Status
- State: IN PROGRESS
- Tickets: CCR-006, CCR-008, CCR-009 (gating later removed in CCR-024), CCR-010, CCR-014 (planned), CCR-018, CCR-019, CCR-020, CCR-022, CCR-023, CCR-024, CCR-026, CCR-027 (deferred), CCR-028 (AUQ collision), CCR-030, CCR-031, CCR-032, CCR-033, CCR-034, CCR-035, CCR-036, CCR-037, CCR-038, CCR-039 (planned), CCR-040 (planned)
- Last updated: CCR-038 (2026-05-06)
