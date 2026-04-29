# Context: chat-bot

## Files
- `src/ccr/bot/__init__.py` — empty package marker
- `src/ccr/bot/handlers/__init__.py` — empty package marker
- `src/ccr/bot/middlewares.py` — `AllowlistMiddleware`: extracts sender from Message/CallbackQuery, checks `auth.allowlist.is_paired`, injects `is_paired_user` into aiogram data, persists `last_chat_id` on paired senders, short-circuits non-`/start` traffic from unpaired senders
- `src/ccr/bot/notify.py` — `notify_owner` (queries owner row, sends to `last_chat_id`, warns if no owner/null chat_id); `broadcast_paired` (fans out to all paired users with non-null `last_chat_id`, skips `exclude` set, swallows per-recipient `TelegramAPIError`)
- `src/ccr/bot/handlers/pairing.py` — `/start` handler with three branches: paired status reply; bootstrap (no owner) reply with code + approve command; normal path reply + `notify_owner` with code. Note: username is interpolated unescaped in HTML mode (F1 advisory — harden with `html.escape()` when adding more HTML messages in CCR-008)
- `src/ccr/bot/app.py` — `build_dispatcher(settings, db_factory, session_manager=None) -> tuple[Bot, Dispatcher]` (registers `AllowlistMiddleware`, stashes `db_factory` and `session_manager` in workflow data, includes pairing router and session router); `run_polling` (logs `"Bot started, awaiting updates"` then enters `dp.start_polling(bot)`)
- `src/ccr/bot/formatting.py` — `chunk_text` and `event_to_messages(event: ClaudeEvent) -> list[str]`; result line: `✅ done · {s}s · {N}k tokens` (no `$cost`; seconds-format duration; optional token segment from `ResultUsage`); all user-controlled strings escaped via `html.escape()`
- `src/ccr/bot/handlers/session.py` — `/new`, `/stop`, `/clear`, `/who`, `/pid` command handlers plus `handle_text` plain-text handler; `/new` and `/clear` replies include `(pid <N>)`; `/pid` shows session id, pid, and uptime
- `src/ccr/bot/typing.py` — `TypingKeepalive` class: per-chat task sending `send_chat_action("typing")` every 4s while a session is producing events; swallows `TelegramAPIError`; lifecycle mirrors `_ChatSender` (`start`/`cancel`/`wait_closed`)
- `src/ccr/server.py` — `async serve(settings)` runs bot polling, `_broadcast_loop`, and `_typing_loop` concurrently; `_ChatSender` per-chat queue; `TypingKeepalive` tasks started/cancelled per session event stream
- `src/ccr/cli.py` — `_cmd_serve` wires `serve` subcommand to `asyncio.run(server.serve(Settings()))`
- `tests/test_bot_pairing.py` — 7 tests covering all four acceptance paths: bootstrap, normal, already-paired, unpaired plain text rejection, plus middleware pass-through scenarios
- `tests/test_typing.py` — 8 tests covering `TypingKeepalive` firing, looping, error swallowing, idempotent cancel/start, and `_typing_loop` integration

## Relations
- depends on: auth, core, claude-runtime

## Change history
- [CCR-006]: bot scaffold + allowlist middleware + /start pairing flow + serve() polling entrypoint
- [CCR-008]: session lifecycle handlers (/new /stop /clear /who + plain-text forwarding), ClaudeEvent → Telegram HTML formatter with chunking, per-chat queue broadcast loop wired into serve()
- [CCR-018]: PID exposure via /new, /clear, /pid; result line rewritten (no $cost, seconds-format duration, optional token segment); new typing.py keepalive module wired as _typing_loop in serve()
