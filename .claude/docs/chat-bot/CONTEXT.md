# Context: chat-bot

## Files
- `src/ccr/bot/__init__.py` — empty package marker
- `src/ccr/bot/handlers/__init__.py` — empty package marker
- `src/ccr/bot/middlewares.py` — `AllowlistMiddleware`: extracts sender from Message/CallbackQuery, checks `auth.allowlist.is_paired`, injects `is_paired_user` into aiogram data, persists `last_chat_id` on paired senders, short-circuits non-`/start` traffic from unpaired senders
- `src/ccr/bot/notify.py` — `notify_owner` (queries owner row, sends to `last_chat_id`, warns if no owner/null chat_id); `broadcast_paired` (fans out to all paired users with non-null `last_chat_id`, skips `exclude` set, swallows per-recipient `TelegramAPIError`)
- `src/ccr/bot/handlers/pairing.py` — `/start` handler with three branches: paired status reply; bootstrap (no owner) reply with code + approve command; normal path reply + `notify_owner` with code. Note: username is interpolated unescaped in HTML mode (F1 advisory — harden with `html.escape()` when adding more HTML messages in CCR-008)
- `src/ccr/bot/app.py` — `build_dispatcher(settings, db_factory) -> tuple[Bot, Dispatcher]` (registers `AllowlistMiddleware` on both `dp.message` and `dp.callback_query`, stashes `db_factory` in workflow data, includes pairing router); `run_polling` (logs `"Bot started, awaiting updates"` then enters `dp.start_polling(bot)`)
- `src/ccr/server.py` — `async serve(settings)` builds engine + `db_factory`, calls `build_dispatcher` + `run_polling`, disposes engine in finally (polling-only; uvicorn added in CCR-012)
- `src/ccr/cli.py` — `_cmd_serve` wires `serve` subcommand to `asyncio.run(server.serve(Settings()))`
- `tests/test_bot_pairing.py` — 7 tests covering all four acceptance paths: bootstrap, normal, already-paired, unpaired plain text rejection, plus middleware pass-through scenarios

## Relations
- depends on: auth, core

## Change history
- [CCR-006]: bot scaffold + allowlist middleware + /start pairing flow + serve() polling entrypoint
