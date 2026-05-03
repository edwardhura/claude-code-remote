# Plan: CCR-031 — `/clear` command — reset session and clear chat history

## Goal

Extend the existing `cmd_clear` so that, in addition to its current "stop the active session and start a fresh one" behaviour, it posts a visual `--- new session ---` divider to every paired chat. This gives users a clear "the old conversation is over, what follows is new" boundary without introducing a new message-id-tracking subsystem that Telegram's bot API constraints would make unreliable anyway.

After this ticket lands: `/clear` from any paired user (i) stops the running Claude session (if any), (ii) broadcasts a divider line to every paired chat with a known `last_chat_id`, and (iii) starts a fresh session and replies with the existing `"Session <id8> started (pid <pid>)."` acknowledgement to the invoker. Failure to broadcast the divider to one or more chats does not block the session reset or the acknowledgement reply.

## UX choice (load-bearing)

**Picked: option (b) — divider-only, no message deletion.**

Rejected (a) and (c) because they require introducing a new cross-context abstraction (per-chat message-id tracking populated by `_ChatSender._run` and threaded into `cmd_clear` via aiogram workflow data), and Telegram bot-API constraints make the deletion behaviour unreliable enough that the abstraction's payoff is small:

- `bot.delete_message` only works for messages < 48 hours old. Any session that has been running for a day or has older bot messages above it cannot be cleared.
- An in-memory ring buffer is wiped on every bot restart. Common operator workflow (restart the service, then `/clear`) would silently delete nothing.
- Broadcast messages go to *every* paired user's chat — tracking is per-`(chat_id, message_id)`, not per-message-id. A 3-paired-user setup with a 30-message session is 90 `delete_message` calls per `/clear`, each subject to its own rate-limit handling.
- The user's stated ask was "start new session and clear chat history". On Telegram, `clear chat history` is a feature only the user can invoke client-side; a bot cannot wipe a chat. The divider is the most honest UX the bot API supports.

CLAUDE.md mandates "Default to no new abstraction unless the ticket already produces three concrete callers." This ticket has zero other callers for a `MessageTracker`. PM also notes "in-memory tracking is the default" — meaning PM expects in-memory at most, not that tracking is required. Going with (b) keeps the diff to a ~25-line change in two files plus tests, no new modules, no new `dp[...]` injections.

## File layout

- `src/ccr/bot/handlers/session.py` — **modify**
  - Extend `cmd_clear` to broadcast the divider to all paired chats between the `manager.stop()` and `manager.new_session()` calls. Order: stop → broadcast divider → start new session → reply to invoker. Skip the divider broadcast on the "was already idle" path so a `/clear` from a clean state does not post a misleading "new session" separator with nothing above it.
  - Inject the broadcast `Bot` instance via aiogram workflow data (see "Dependencies" below). The handler grabs it from `dp["bot"]` (added in `app.py`); `db_factory` is already injected.
  - Use `ccr.bot.notify.broadcast_paired` for the actual fan-out so per-chat `TelegramAPIError` swallowing is reused (it already does exactly this).

- `src/ccr/bot/app.py` — **modify** (one-liner addition)
  - Stash the constructed `Bot` instance in workflow data: `dp["bot"] = bot` inside `build_dispatcher`. Today the dispatcher injects `settings`, `db_factory`, and (optionally) `session_manager`; handlers receive them through aiogram parameter binding. The divider broadcast in `cmd_clear` needs an aiogram `Bot` to call `notify.broadcast_paired(bot, db, ...)` — `msg.bot` is also available on the incoming `Message`, so this `dp["bot"]` injection is **optional**: developer may use `msg.bot` instead and skip the `app.py` change entirely. Both patterns are acceptable; pick the one that reads cleaner. The plan's "Public surface" sketch below uses `msg.bot` to keep the diff tighter.

- `tests/test_bot_session.py` — **modify**
  - Extend the existing `/clear` test to assert the divider broadcast was attempted (now uses a real DB factory with seeded paired users + a mocked `Bot.send_message`).
  - Add new test cases listed under "Test surface" below.

- `src/ccr/bot/handlers/clear.py` — **NOT created.** The diff is small (~15 lines added to `cmd_clear`) — splitting into a sibling module would be premature. Same handler, same Router, same test file.

- `src/ccr/claude/manager.py` — **NOT modified.** No new lifecycle method needed. The existing `stop()` + `new_session()` pair is the right primitive; the new behaviour is purely cosmetic at the bot layer.

- No DB migration. No new module. No `_ChatSender` change. No `serve()` change.

## Public surface

```python
# Sketch — illustrative, not the final code.

# src/ccr/bot/handlers/session.py

_DIVIDER_MESSAGE = "— — — new session — — —"


@router.message(Command("clear"))
async def cmd_clear(
    msg: Message,
    session_manager: SessionManager,
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Stop any running session, post a divider, then start a fresh one."""
    if msg.from_user is None:
        return

    prior_status = await session_manager.status()
    await session_manager.stop()

    # Only post the divider when there was actually a session to clear.
    # /clear from a clean state should not pretend a new session boundary
    # exists when nothing came before it.
    if prior_status != SessionStatus.IDLE and msg.bot is not None:
        async with db_factory() as db:
            await broadcast_paired(msg.bot, db, _DIVIDER_MESSAGE)

    try:
        session_id = await session_manager.new_session(
            prompt=None,
            started_by_tg_user_id=msg.from_user.id,
        )
    except SessionError as exc:
        await msg.answer(html.escape(str(exc)))
        return

    inf = await session_manager.info()
    pid = inf.get("pid")
    await msg.answer(f"Session {_short_id(session_id)} started (pid {pid}).")
```

Notes on the sketch:

- `_DIVIDER_MESSAGE` is a module-level constant near the existing `_EMPTY_SESSIONS_REPLY` constants. Exact text is the architect's pick — em-dashes render compactly on Telegram and survive both HTML and plain modes. Tests should match it verbatim.
- `broadcast_paired(bot, db, message)` is the existing helper from `src/ccr/bot/notify.py`. It handles the "skip null `last_chat_id`", "skip revoked", and "swallow per-chat `TelegramAPIError`" paths already. No new code needed.
- `prior_status` capture happens **before** `manager.stop()`. After `stop()` the in-memory status flips to `STOPPED` (or whatever `_await_exit` decided), and `manager.status()` would still return non-`IDLE` until the proc reference is cleared, but reading it before `stop()` is unambiguous and avoids any race between `_teardown_locked` and our read.
- The divider broadcast happens **before** `manager.new_session()` so the new session's first events (init, first prompt, etc.) appear after the divider in chat order. If we posted the divider after `new_session`, paired users could see the new session's first event interleaved with the divider depending on broadcast loop scheduling.
- The acknowledgement reply (`Session <id8> started (pid <pid>).`) is unchanged. The existing test (`test_cmd_clear_stops_then_starts_fresh`) keeps its acceptance string with one tweak: it now also has paired users seeded so the divider broadcast is exercised.

## Patterns and prior art

- **Reuse:** `src/ccr/bot/notify.py:47-78` — `broadcast_paired(bot, db, message, exclude=None)` already does the per-chat fan-out with `TelegramAPIError` swallowing. The whole acceptance bullet about "Telegram API failures during message deletion are caught and ignored" maps onto its existing per-recipient try/except. We do not need to write new error-handling code; we reuse the already-tested helper.
- **Reuse:** `src/ccr/bot/handlers/session.py:150-170` — current `cmd_clear` is the baseline; we extend rather than replace. Reply string preserved verbatim so existing test regression coverage continues to pass with the addition of paired-user seeding.
- **Reuse:** `db_factory` injection pattern from existing handlers (`/sessions`, `/who`) — open a short-lived async session inside the handler, do the work, commit-eager-or-noop. `broadcast_paired` opens its own iteration over the SQL result; we just hand it the session.
- **Avoid:** introducing a `MessageTracker` abstraction. CLAUDE.md's "default to no new abstraction" rule applies — there is one caller, and the API constraints make the abstraction's payoff marginal. If a future ticket adds a second concrete caller for tracked message-ids (e.g. "edit-the-last-bot-message-on-event-X"), then revisit and lift it then.
- **Avoid:** routing the divider through the `EventBus` as a synthetic event. It is not a Claude session event, has no `seq`, and would muddle JSONL-on-disk semantics for the SSE viewer (CCR-012). Use the direct broadcast helper.
- **Avoid:** posting the divider via `msg.answer` (which would only reach the invoker's chat). Multi-paired-user setups need the divider to land in every chat that received the prior session's events.

## Abstractions

**No new abstraction.** `cmd_clear` gains ~10 lines that compose two existing helpers (`session_manager.stop` / `session_manager.new_session`) with one existing helper (`notify.broadcast_paired`). All four already exist and are tested. The PM-flagged "message-id tracking is the open question" is resolved by picking option (b), which does not need it.

If the team decides at review time to also include option (a) on top of (b) (i.e. opt for hybrid (c) after all), the plan would need a follow-up: add a `MessageTracker` (per-chat ring buffer with `cap=200` or so) populated from `_ChatSender._run` after each successful `bot.send_message` returns a `Message`, threaded into the dispatcher via `dp["message_tracker"] = tracker`, and consumed in `cmd_clear` after the divider broadcast. That is deliberately out of scope for this ticket per the UX choice above.

## Dependencies

- Depends on: `src/ccr/bot/notify.py` (`broadcast_paired`); `src/ccr/claude/manager.py` (existing `stop`, `new_session`, `status`, `info` — no changes); `src/ccr/claude/state.py` (`SessionStatus.IDLE`).
- Used by: nothing else — `/clear` is a leaf user-facing command.
- No EventBus topic added or consumed.
- No DB schema change.
- No settings additions.

## Edge cases the developer must handle

- **`msg.from_user is None`**: existing handler already returns early; preserve this.
- **`/clear` with no active session (idle)**: `manager.stop()` is documented idempotent and no-ops. The new code path must NOT post a divider in this case (acceptance criterion: "still produces a clean acknowledgement (no crash, no spurious deletion errors)" — a divider with nothing above it is the moral equivalent of a "spurious" event). Use the `prior_status != SessionStatus.IDLE` gate.
- **`SessionError` from `new_session`** (e.g. claude binary missing): existing handler's `try/except SessionError` path stays. The divider has already been posted at this point and is fine to remain in chat — the user will see "divider, then error" which still communicates "old session ended; new one failed to start".
- **`broadcast_paired` raising**: by contract it does not raise on per-chat failures (it logs and continues). DB-layer errors (e.g. SQLite locked) would still bubble. Treat any unexpected exception as fatal to the handler so it surfaces in logs; do not wrap with a blanket `except` that hides bugs. The acceptance "Telegram API failures during message deletion are caught and ignored" maps to the existing per-recipient `TelegramAPIError` swallow inside `broadcast_paired`, which is already covered by `tests/test_bot_pairing.py` indirectly.
- **`msg.bot is None`**: defensive — aiogram's `Message.bot` is populated by the dispatcher before our handler runs, so this is effectively unreachable in production. Skip the broadcast and proceed with the session reset on this branch (don't crash). The alternative is to inject the `Bot` via `dp["bot"]`; either works.
- **Concurrency**: `cmd_clear` is invoked from the aiogram polling task; `manager.stop()` and `manager.new_session()` each take `_session_lock` and serialise correctly. Two paired users tapping `/clear` simultaneously is fine — the second one sees a still-running session (just-started by the first), stops it, posts another divider, starts another. PM did not flag this as out-of-scope-bad behaviour; current `cmd_clear` already exhibits it.
- **No paired users with `last_chat_id`**: `broadcast_paired` no-ops with an empty SELECT result. No special-casing needed.
- **Divider text formatting**: the bot is configured `parse_mode="HTML"` by default (`DefaultBotProperties` in `app.py:60`), and `broadcast_paired` passes `parse_mode="HTML"`. Use plain ASCII-safe characters in `_DIVIDER_MESSAGE` so HTML escaping is moot — em-dashes (`—`) and the word `new session` need no escaping. Do not interpolate any user-controlled values.

## Test surface

All changes land in `tests/test_bot_session.py`. The existing `test_cmd_clear_stops_then_starts_fresh` is updated (it currently uses `FakeManager` and asserts the reply only — needs paired-user seeding to exercise the broadcast path) plus three new tests:

- `tests/test_bot_session.py::test_cmd_clear_stops_then_starts_fresh` — **update**: keep the existing `manager.stop` / `manager.new_session` / reply assertions; add a real `bot` mock, seed two paired users with `last_chat_id`, assert `bot.send_message` was awaited twice with the divider text in the correct chat ids and the running-session acknowledgement was sent to the invoker.
- `tests/test_bot_session.py::test_cmd_clear_idle_skips_divider` — **add**: `FakeManager` with `status=IDLE`, paired users seeded; run `cmd_clear`; assert `bot.send_message` was NOT awaited for the divider (the `msg.answer` for the new-session reply is not the same call site, so this assertion is precise — `msg.bot.send_message` mock vs. `msg.answer` mock); assert `manager.new_session` still ran and the acknowledgement reply was sent.
- `tests/test_bot_session.py::test_cmd_clear_swallows_broadcast_failures` — **add**: paired users seeded, `bot.send_message` raises `TelegramAPIError` for one chat id and succeeds for the other; assert `cmd_clear` still completes (no exception escapes), `manager.new_session` was awaited, the acknowledgement reply was sent, and the second chat received the divider despite the first failing. Mirrors `test_broadcast.py::test_broadcast_swallows_telegram_errors_per_chat`.
- `tests/test_bot_session.py::test_cmd_clear_no_paired_users_with_chat_id_completes` — **add**: zero paired users (or all with `last_chat_id IS NULL`), running session; assert `cmd_clear` completes, no broadcast sends are attempted, acknowledgement reply still sent.

The `bot` argument enters the handler via `msg.bot`. To make this testable, `_make_message` in the test file must be extended to accept and attach a `bot` mock to the `Message` (today it stubs only `msg.answer`). Use `object.__setattr__(msg, "bot", bot_mock)` mirroring the existing `object.__setattr__(msg, "answer", AsyncMock())` line.

The reviewer's existing acceptance commands stay the same: `pytest tests/test_bot_session.py` and the project-level `pytest --cov=ccr --cov-fail-under=80`. No changes to `tests/fakes/`.

## Out of scope

- Deleting bot-posted messages, regardless of age. The 48h API constraint plus the cross-restart wipe make this unreliable; we explicitly do not do it (this is the team-lead-promised "if (a) is rejected, document why").
- Persisting message-ids across restarts (DB column / migration). Not needed because we do not delete.
- Editing or removing existing chat content client-side — Telegram does not let bots invoke "clear chat history".
- Touching `/new` or `/continue` semantics. They keep their own handlers and current behaviour.
- A `MessageTracker` abstraction or any aiogram outgoing-message middleware. PM's "if (a) or (c), this ticket needs to introduce simple message-id tracking" branch does not fire because we picked (b).
- Modifying `_ChatSender._run` to capture `Message` returns from `bot.send_message`. The existing discard-the-return-value pattern stays.
- Updating `dp[...]` injections in `build_dispatcher` (aside from the optional `dp["bot"] = bot` one-liner mentioned above). The recommended path is to use `msg.bot` and not touch `app.py` at all.
- Persisting / displaying a "cleared at" timestamp anywhere.
- Per-chat configuration of divider text or "skip divider on idle" behaviour — both are wired as constants.

## Open questions for team lead

None. The UX choice is the architect's call per the dispatch brief, and the design above resolves it without leaving open questions for the developer or the team lead. Two minor judgment calls (em-dash divider text vs. ASCII alternative; `msg.bot` vs. `dp["bot"]` injection) are both flagged as developer's choice in the plan and either is acceptable.
