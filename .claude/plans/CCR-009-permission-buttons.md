# Plan: CCR-009 — Permission inline-button handling

## Goal

Make `permission_request` events from Claude Code actionable in Telegram. When the wrapper publishes a `PermissionRequest`, the broadcast loop must render it as a single Telegram message with one inline button per offered option. Tapping a button forwards the choice to the running subprocess via `SessionManager.send_permission` and edits the message to remove the keyboard plus append `"→ {choice} (by @{username})"` so every paired user sees who answered. While a `permission_request` is outstanding, **Telegram delivery only** is paused — buffered events are flushed in order once the response is recorded. SSE consumers (CCR-012) keep streaming live; the buffer lives entirely inside the broadcast loop.

The ticket also widens `OutboundMessage` from a bare `str` to `tuple[str, InlineKeyboardMarkup | None]`, threading the keyboard from the formatter through `_broadcast_loop` and `_ChatSender` so non-permission events still work unchanged.

## File layout

- `src/ccr/bot/keyboards.py` — **create** — `permission_kb(session_id, request_id, options) -> InlineKeyboardMarkup` only. New module: nothing else needs a keyboard yet, but a separate module avoids polluting `formatting.py` with aiogram widget construction and gives CCR-014 (`/view`, `/last`, `/preview`) a natural home for future link buttons.
- `src/ccr/bot/formatting.py` — **modify** — widen `OutboundMessage` to `tuple[str, InlineKeyboardMarkup | None]`; refactor every existing producer to return `(text, None)`; add a `PermissionRequest` branch that returns `(text, permission_kb(...))`.
- `src/ccr/bot/handlers/permission.py` — **create** — single `@router.callback_query(F.data.startswith("perm:"))` handler.
- `src/ccr/bot/app.py` — **modify** — `dp.include_router(permission_router)` wired into `build_dispatcher`. (One-line addition next to the existing `pairing_router` / `session_router` includes.)
- `src/ccr/claude/manager.py` — **modify** — add `_pending_permissions: dict[str, asyncio.Event]` keyed by `request_id`, plus `_telegram_pause_count: dict[uuid.UUID, int]` keyed by `session_id`, plus per-session `_telegram_resume: dict[uuid.UUID, asyncio.Event]`. Set on `PermissionRequest` publish in `_consume_events`; clear in `send_permission`. Add `is_telegram_paused(session_id)` and `wait_for_resume(session_id)` accessors. Also store the latest `PermissionRequest.options` per `request_id` so the callback handler can authoritatively validate `choice` (see "Decisions / Permission validation source of truth").
- `src/ccr/server.py` — **modify** — `_broadcast_loop` learns the new `OutboundMessage` shape, holds a per-loop `list[OutboundMessage]` buffer, and consults `manager.is_telegram_paused(session_id)` before flushing. `_ChatSender.enqueue` accepts the `OutboundMessage` tuple; `_ChatSender._run` passes `reply_markup` to `bot.send_message`. The broadcast loop now needs the `SessionManager` reference (currently it has only the bus, bot, and `db_factory`) — wire `manager` through `serve()` into `_broadcast_loop` and `_ChatSender` is unchanged in dependencies.
- `tests/test_bot_permission.py` — **create** — handler-level tests (mocked bot + mocked manager + in-memory DB).
- `tests/test_formatting.py` — **modify** — every existing assertion updated for the new tuple shape; new test for `PermissionRequest` returning `(text, keyboard)` with the right `callback_data`.
- `tests/test_broadcast.py` — **modify** — existing assertions updated for the new tuple shape; new test for the buffering / drain behavior end-to-end through `_broadcast_loop`.
- `tests/test_session_manager.py` — **modify** — new tests for `is_telegram_paused` / `send_permission` clearing the gate / multiple concurrent requests.

The ticket's `Files:` block lists six items; the plan adds three modifications to existing test files (existing assertions become incorrect when `OutboundMessage` widens) plus the dispatcher wire-up in `bot/app.py`. These are not new ticket scope — they fall out mechanically from the cascade and the architect's brief.

## Public surface

### `src/ccr/bot/keyboards.py`

```python
# Sketch — illustrative, not the final code.
from __future__ import annotations

import uuid

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Friendly labels for the canonical Claude permission options. Anything not
# in this map falls back to ``opt.capitalize()`` so unknown options still
# render. Original ``opt`` strings are preserved verbatim in callback_data.
_LABELS: dict[str, str] = {
    "approve": "Approve",
    "skip": "Skip",
    "abort": "Abort",
}


def permission_kb(
    session_id: uuid.UUID,
    request_id: str,
    options: list[str],
) -> InlineKeyboardMarkup:
    """One row per option. callback_data == ``perm:{session_id}:{request_id}:{choice}``."""
    rows = [
        [
            InlineKeyboardButton(
                text=_LABELS.get(opt, opt.capitalize()),
                callback_data=f"perm:{session_id}:{request_id}:{opt}",
            ),
        ]
        for opt in options
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

Notes:
- Telegram caps `callback_data` at 64 bytes. `session_id` is a UUID (36 chars) and the `perm:` prefix + two `:` + a typical option (`approve`) is well under 64. We do not truncate, but the handler validates the parse and falls through to "stale" rather than crashing if a malformed payload arrives. (Worst case in practice: a 36-char UUID + `perm:` (5) + `request_id` + 2 `:` + option. If `request_id` is long the cap can trip; we accept that risk because the upstream `request_id` is generally short, and we will revisit if real Claude Code sessions surface long ids.)

### `src/ccr/bot/formatting.py`

```python
# Sketch — illustrative, not the final code.
from aiogram.types import InlineKeyboardMarkup

# Widened — every event now carries an optional keyboard.
type OutboundMessage = tuple[str, InlineKeyboardMarkup | None]


def event_to_messages(event: ClaudeEvent) -> list[OutboundMessage]:
    if isinstance(event, AssistantTurn):
        return [(text, None) for text in _format_assistant_turn(event)]
    if isinstance(event, ResultEvent):
        return [(text, None) for text in _format_result(event)]
    if isinstance(event, PermissionRequest):
        return [_format_permission(event)]
    # SystemInit, UserTurn, UnknownEvent → []
    return []


def _format_permission(event: PermissionRequest) -> OutboundMessage:
    tool = html.escape(event.tool_name)
    # Truncate the tool input the same way tool_use does (≤ 200 chars repr).
    args = html.escape(repr(event.input)[:_TOOL_ARG_TRUNCATE])
    text = (
        f"\U0001f6d1 Permission requested\n"
        f"Tool: <code>{tool}</code>\n"
        f"Input: {args}"
    )
    # session_id wired in by the broadcast loop? No — the formatter sees only
    # the event. The broadcast loop has the session_id from the bus payload.
    # See "Decisions / Where does the keyboard's session_id come from".
    return (text, _PendingKeyboard(event.request_id, event.options))
```

The decision on how `session_id` reaches the keyboard is documented below ("Decisions / Where does the keyboard's session_id come from"). Spoiler: the formatter returns a sentinel and the broadcast loop materialises the real `InlineKeyboardMarkup` because the `bus.publish` payload is the only place the `session_id` lives.

### `src/ccr/bot/handlers/permission.py`

```python
# Sketch — illustrative, not the final code.
from __future__ import annotations

import html
import uuid
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.types import CallbackQuery

from ccr.claude.manager import (
    NoActiveSessionError,
    SessionError,
    SessionManager,
    StaleSessionError,
)

router = Router(name="permission")


@router.callback_query(F.data.startswith("perm:"))
async def cb_permission(
    cb: CallbackQuery,
    session_manager: SessionManager,
    is_paired_user: bool,
) -> None:
    if not is_paired_user:
        await cb.answer("Not paired.", show_alert=True)
        return
    if cb.data is None or cb.from_user is None:
        return  # defensive — aiogram should not deliver these
    parsed = _parse_callback(cb.data)
    if parsed is None:
        await cb.answer("Stale prompt", show_alert=True)
        return
    session_id, request_id, choice = parsed

    # Authoritative validation: ask the manager whether this request_id is
    # still pending and whether ``choice`` is in its option set.
    if not session_manager.is_permission_choice_valid(request_id, choice):
        await cb.answer("Stale prompt", show_alert=True)
        return

    try:
        await session_manager.send_permission(session_id, request_id, choice)
    except StaleSessionError:
        await cb.answer("Stale prompt", show_alert=True)
        return
    except NoActiveSessionError:
        await cb.answer("No active session", show_alert=True)
        return
    except SessionError:
        await cb.answer("Failed to record choice", show_alert=True)
        return

    username = cb.from_user.username or str(cb.from_user.id)
    suffix = f"\n→ {html.escape(choice)} (by @{html.escape(username)})"
    if cb.message is not None:
        original_text = cb.message.html_text or cb.message.text or ""
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.edit_text(original_text + suffix, reply_markup=None)
    await cb.answer()
```

### `src/ccr/claude/manager.py`

```python
# Sketch — illustrative, not the final code.
class SessionManager:
    def __init__(self, ...) -> None:
        ...
        # request_id → asyncio.Event signalled when the choice arrives.
        # NOT used to gate Telegram delivery — that's _telegram_resume.
        self._pending_permissions: dict[str, asyncio.Event] = {}
        # request_id → frozenset of allowed choices captured from the
        # PermissionRequest event so the callback handler can validate
        # ``choice`` without trusting the (forgeable) callback_data.
        self._pending_options: dict[str, frozenset[str]] = {}
        # session_id → number of outstanding permission_requests. > 0 means
        # the broadcast task should buffer Telegram delivery.
        self._telegram_pause_count: dict[uuid.UUID, int] = {}
        # session_id → asyncio.Event SET when broadcast may flow, CLEARED
        # while a permission is outstanding. Created lazily on the first
        # PermissionRequest for a session.
        self._telegram_resume: dict[uuid.UUID, asyncio.Event] = {}

    def is_telegram_paused(self, session_id: uuid.UUID) -> bool:
        return self._telegram_pause_count.get(session_id, 0) > 0

    async def wait_for_resume(self, session_id: uuid.UUID) -> None:
        ev = self._telegram_resume.get(session_id)
        if ev is None or ev.is_set():
            return
        await ev.wait()

    def is_permission_choice_valid(self, request_id: str, choice: str) -> bool:
        opts = self._pending_options.get(request_id)
        return opts is not None and choice in opts

    async def send_permission(
        self,
        session_id: uuid.UUID,
        request_id: str,
        choice: str,
    ) -> None:
        # Existing checks (NoActiveSessionError, StaleSessionError) stay.
        ...
        await self._proc.send_permission_response(request_id, choice)
        self._clear_pending_permission(session_id, request_id)

    def _clear_pending_permission(
        self,
        session_id: uuid.UUID,
        request_id: str,
    ) -> None:
        # Drop the request from the pending dicts (idempotent — duplicate taps
        # find nothing to clear).
        self._pending_options.pop(request_id, None)
        ev = self._pending_permissions.pop(request_id, None)
        if ev is not None:
            ev.set()
        # Decrement session counter and unpause Telegram if it hits 0.
        count = self._telegram_pause_count.get(session_id, 0)
        if count <= 1:
            self._telegram_pause_count.pop(session_id, None)
            resume = self._telegram_resume.get(session_id)
            if resume is not None:
                resume.set()
        else:
            self._telegram_pause_count[session_id] = count - 1
```

In `_consume_events`, on each `PermissionRequest` (after `log.append` / `bus.publish`):

```python
# Sketch — illustrative.
if isinstance(event, PermissionRequest):
    self._pending_options[event.request_id] = frozenset(event.options)
    self._pending_permissions[event.request_id] = asyncio.Event()
    self._telegram_pause_count[session_id] = (
        self._telegram_pause_count.get(session_id, 0) + 1
    )
    resume = self._telegram_resume.get(session_id)
    if resume is None:
        resume = asyncio.Event()
        resume.set()  # default: not paused
        self._telegram_resume[session_id] = resume
    resume.clear()  # now paused
```

On session teardown (`_teardown_locked`), drop all gating state for the dying session_id so a stale resume Event doesn't leak into the next session: `_telegram_pause_count.pop`, `_telegram_resume.pop`, and clear `_pending_permissions` / `_pending_options` for any request that was outstanding. Also `set()` any orphaned resume Events first to wake stalled `_broadcast_loop` waiters.

### `src/ccr/server.py`

```python
# Sketch — illustrative, not the final code.
async def _broadcast_loop(
    bus: EventBus,
    bot: Bot,
    db_factory: async_sessionmaker[AsyncSession],
    manager: SessionManager,                # NEW — needed for is_telegram_paused
) -> None:
    senders: dict[int, _ChatSender] = {}
    buffered: list[tuple[uuid.UUID, list[OutboundMessage]]] = []
    try:
        async for payload in bus.subscribe("session.event"):
            if not isinstance(payload, dict):
                continue
            event = payload.get("event")
            session_id = payload.get("session_id")
            if event is None or session_id is None:
                continue
            messages = event_to_messages(event)
            # Materialise sentinels into real keyboards. The formatter doesn't
            # know session_id; the loop does.
            messages = _materialise_keyboards(messages, session_id)
            if not messages:
                # Buffer empty-message events too? No — they produce nothing
                # to send anyway. Nothing to buffer.
                continue

            if isinstance(event, PermissionRequest):
                # The PermissionRequest message itself MUST be flushed
                # immediately (otherwise users have nothing to tap and the
                # gate never lifts). Flush it before the gate is closed.
                await _flush_buffer(buffered, senders, bot, db_factory)
                await _send_to_all(messages, senders, bot, db_factory)
                continue

            # Was Telegram delivery paused for this session?
            if manager.is_telegram_paused(session_id):
                buffered.append((session_id, messages))
                continue

            # Not paused — but if the buffer has anything for this session,
            # flush it first so order is preserved.
            await _flush_buffer(buffered, senders, bot, db_factory, session_id=session_id)
            await _send_to_all(messages, senders, bot, db_factory)
    finally:
        ...
```

Subtlety: the loop must NOT block on `manager.wait_for_resume()` inside the `async for` body because the bus generator is still producing events that need to be ingested into `buffered`. The simplest correct shape: poll `is_telegram_paused()` per event, append to `buffered` while paused, and flush the buffer the first time we see an event arrive while `is_telegram_paused()` is False. The first non-paused event after a permission response acts as the drain trigger. This is a deliberate trade-off — see "Edge cases / Drain timing".

Alternative considered (rejected): `await manager.wait_for_resume(session_id)` inside the loop body. This stalls `bus.subscribe()` consumption and trips the EventBus drop-oldest backpressure. We do not want that.

`_ChatSender` changes:

```python
# Sketch — illustrative.
class _ChatSender:
    def enqueue(self, message: OutboundMessage) -> None:
        self._queue.put_nowait(message)

    async def _run(self) -> None:
        while True:
            text, kb = await self._queue.get()
            try:
                await self._bot.send_message(
                    self._chat_id,
                    text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            except TelegramAPIError as exc:
                ...
```

## Patterns and prior art

- **Reuse**: `src/ccr/bot/handlers/session.py` (handler-with-`session_manager` shape) and `src/ccr/bot/handlers/pairing.py` (router-per-handler module). The new `permission.py` mirrors both: one router named `permission`, one handler, accesses `session_manager` via aiogram workflow data.
- **Reuse**: `src/ccr/bot/middlewares.py:34-46` already extracts paired status for `CallbackQuery` updates and short-circuits unpaired senders. The permission handler does NOT need its own paired check (the middleware will inject `is_paired_user`); we keep the explicit guard inside the handler as a belt-and-suspenders fail-closed check, matching the pattern in `cmd_who`.
- **Reuse**: `src/ccr/server.py:175-232` `_ChatSender` queue-per-chat shape. The widening from `str` to tuple is mechanical; the per-chat queue, the `start`/`cancel`/`wait_closed` lifecycle, and the swallow-`TelegramAPIError` pattern stay identical.
- **Reuse**: `src/ccr/claude/manager.py:305-330` `_consume_events` — the new `PermissionRequest` handling sits next to the existing `SystemInit` / `ResultEvent` branches. Same publish-then-bookkeep structure.
- **Extend**: `src/ccr/bot/formatting.py` — same dispatch-on-event-type structure; one new branch.
- **Avoid**: a per-`_ChatSender` pause/resume API. The buffer must live in `_broadcast_loop` so SSE (which consumes `bus.subscribe` independently) keeps flowing and so the per-chat queues don't accumulate stalled events. The team-lead brief is explicit on this.
- **Avoid**: trusting `callback_data` alone for the allowed choice set. We capture the option set from the original `PermissionRequest` event in `SessionManager` and validate against that. Rationale: `callback_data` is opaque to the user but trivially forgeable by anyone with shell access to the Telegram client; we already have the authoritative options in memory.

## Abstractions

**No new long-lived abstraction.** The new `keyboards.py` module is a one-function file that exists to keep aiogram widget construction out of `formatting.py`; it is not an abstraction layer, just a sibling. We do not introduce a `Buffer` class or a `PauseGate` class — the `_broadcast_loop` body holds a plain `list[OutboundMessage]` and consults two simple manager methods (`is_telegram_paused`, `is_permission_choice_valid`). A flat dict-of-Events on `SessionManager` is the right size for one outstanding permission per session at the moment, and three concrete callers are not present.

The one borderline case: should `OutboundMessage` become a Pydantic model (`@dataclass`-like) instead of a plain tuple? Decision: stay as `tuple[str, InlineKeyboardMarkup | None]`. Two fields, both load-bearing, no behavior. A named tuple or dataclass would only be justified if a third field (e.g. `parse_mode`, `disable_web_page_preview`) is ever needed; we revisit then. The `OutboundMessage` type alias keeps the call sites readable.

## Dependencies

- Depends on:
  - `ccr.claude.events.PermissionRequest` (already exists in `src/ccr/claude/events.py:146-154`).
  - `ccr.claude.manager.SessionManager.send_permission` (exists; reuse the validation it already does on `session_id`).
  - `ccr.bot.middlewares.AllowlistMiddleware` (already wired onto `dp.callback_query`).
  - `aiogram.types.InlineKeyboardButton` / `InlineKeyboardMarkup` / `CallbackQuery`.
- Used by:
  - `src/ccr/server.py` `_broadcast_loop` — consumes the new tuple shape and the new `is_telegram_paused` predicate.
  - Future CCR-019 may want to display whether a session has an outstanding permission; reading `is_telegram_paused(session_id)` is the natural surface.

## Decisions

### Permission validation source of truth

`SessionManager._pending_options[request_id]` is canonical. The callback handler calls `session_manager.is_permission_choice_valid(request_id, choice)` before `send_permission`. Rationale:
- `callback_data` is forgeable (any client can craft an `?? perm:...` payload with arbitrary `choice`).
- Re-deriving from JSONL via `JsonlSessionLog.read_from(0)` would be correct but slow and crosses module boundaries unnecessarily.
- The manager already owns the single in-memory authority for "what's running"; one more dict is consistent.
- On session teardown, we drop the dict, so a stale tap from before `/clear` correctly fails validation.

If `is_permission_choice_valid` returns `False`, the handler responds with the same "Stale prompt" alert as a stale `session_id`. The user observes one consistent failure mode for "this button is too old".

### Where does the keyboard's `session_id` come from

The formatter sees only the `ClaudeEvent`. The `session_id` comes from the bus payload (`{"session_id", "seq", "event"}`). Two implementation options:

1. **Sentinel pattern (chosen).** `event_to_messages` returns `(text, _PendingKeyboard(request_id, options))` for permission events; `_broadcast_loop` calls a small helper `_materialise_keyboards(messages, session_id)` that swaps the sentinel for `permission_kb(session_id, request_id, options)`. Keeps the formatter a pure function of `ClaudeEvent`.
2. Pass `session_id` into `event_to_messages`. Simpler call site, but every existing caller now must thread a UUID it doesn't otherwise care about.

Option 1 has one extra type but localises the change to the broadcast loop. The `_PendingKeyboard` type is a tiny dataclass private to `formatting.py` (or a `NamedTuple`); the loop's `_materialise_keyboards` is a 6-line function in `server.py`.

Alternative rejected: have the formatter accept the bus payload directly. The formatter is shared with web-viewer rendering work in CCR-013; it should stay event-shaped, not payload-shaped.

### Pause primitive — `asyncio.Event` per session, refcounted

We use a session-keyed counter (`_telegram_pause_count[session_id]`) plus a session-keyed `asyncio.Event` (`_telegram_resume[session_id]`). Multiple concurrent permission requests for the same session (theoretical but possible) increment the counter; only the last `send_permission` (count → 0) flips the resume Event. The per-`request_id` `_pending_permissions` dict is kept separate so we never decrement twice on a duplicate tap.

Rejected: a single global pause flag. With one session globally that would work today but couples gating to the wrong granularity if Phase ≥ 19 ever introduces orphan-reconciliation that has multiple `Session` rows in flight. Cheap to do it right now.

Rejected: `asyncio.Condition` with `notify_all`. Heavier than needed and gets us nothing over an Event for one-shot resume.

### Drain trigger

`_broadcast_loop` does NOT `await manager.wait_for_resume(session_id)` (that would stall bus consumption). Instead it polls `is_telegram_paused(session_id)` per event. While paused, events accumulate in `buffered`. On the first event whose session is no longer paused, the buffer flushes (in order) before the new event sends. This means there is a delivery-latency floor of "next event after the resume" — but in practice the user's choice triggers another Claude event almost immediately (a `tool_use` or `tool_result`), so the buffer drains within ~milliseconds of the response. If a permission is the very last thing a session does, the buffer never drains until session teardown — see "Edge cases / Buffered-on-shutdown".

### `_ChatSender` API

`enqueue(message: OutboundMessage)` — single positional arg with the new tuple type. `_run` unpacks `text, kb = await queue.get()` and passes `reply_markup=kb` to `bot.send_message`. No pause / resume API on `_ChatSender`. Pause lives at `_broadcast_loop`. Existing `_ChatSender` tests in `test_broadcast.py` only assert against `bot.send_message.await_args`, which we update for the new signature.

## Edge cases the developer must handle

- **Stale session_id (post-`/clear`).** A user taps a button from a session that is no longer the active one. `SessionManager.send_permission` already raises `StaleSessionError`; the handler catches it and responds `cb.answer("Stale prompt", show_alert=True)`. The keyboard is NOT removed from the original message in this case — leave the buttons; another paired user with a fresher view may answer.
- **Stale request_id (same session, but the request was already answered).** `is_permission_choice_valid` returns `False`. Same `"Stale prompt"` alert. The keyboard is not edited; the original message will already have been edited by whichever user answered first.
- **Concurrent taps from two paired users.** Both callbacks race into `cb_permission`. The first one through `is_permission_choice_valid` succeeds; `_clear_pending_permission` drops the entry; the second one fails validation and returns "Stale prompt". The first tapper's `edit_text` succeeds; the second's would fail with `TelegramBadRequest` because the message has already been edited — wrap `edit_text` in `contextlib.suppress(TelegramBadRequest)` (or catch the specific aiogram error for "message is not modified").
- **Forged `choice`.** Validation against `_pending_options[request_id]` fails closed. No `send_permission` call.
- **Malformed `callback_data`.** `_parse_callback` returns `None` → `"Stale prompt"` alert. UUIDs are validated with `uuid.UUID(...)`; ValueError → None.
- **Username missing.** `cb.from_user.username` is optional. Fall back to `str(cb.from_user.id)` (i.e. `"@123456"`) so the suffix still includes a stable identifier. HTML-escape it.
- **HTML-escaping the suffix.** The choice and username both go into HTML mode; both must be `html.escape()`-ed. The original message text comes from `cb.message.html_text` (already-rendered HTML) — concatenate, do not re-escape.
- **Edit failure.** Telegram may return "message is not modified" if the user double-taps; suppress `TelegramBadRequest` so the handler still calls `cb.answer()`.
- **Buffered-on-shutdown.** If a session ends (`/stop`) while events are buffered for it, `_teardown_locked` resets the manager's gating state; the buffer in `_broadcast_loop` still holds those messages. Decision: drop them on the floor when the session_id we hold no longer matches the active session and `is_telegram_paused` returns False (i.e. the buffer flushes naturally, and any messages from a closed session simply emit). Alternative considered (and rejected): tie buffer entries to a `session_id` and discard on a `session.status` `STOPPED|CRASHED` event. That requires a second subscription; unnecessary for MVP and the messages are still sent (just possibly after an `idle` reply). Document the trade-off as a comment in `_broadcast_loop`.
- **Multiple permission requests in sequence (same session).** Counter goes 0 → 1 (pause), 1 → 2 (still paused), the user answers one → 2 → 1 (still paused), answers the second → 1 → 0 (resume). This works because we drive the resume Event off the counter, not off "is `_pending_permissions` empty for this session".
- **Manager teardown while a permission is outstanding.** `_teardown_locked` must clear `_pending_options` / `_pending_permissions` for the dying session_id and `set()` the resume Event before dropping it from the dict, so any `wait_for_resume` waiter (if we ever add one) wakes. Currently `_broadcast_loop` polls rather than awaits, so this is defensive only — implement it anyway, it's two lines.
- **`is_telegram_paused` race.** Between the loop's check and its `buffered.append` / send, a `send_permission` could land. Worst case: an event slips through unbuffered while the buffer has older entries. This violates ordering. Mitigation: read the pause flag once per event AND check it again right before `_send_to_all`; if it flipped, append to `buffered` and let the natural drain handle it. Alternatively, hold a single `asyncio.Lock` on the manager around the read-and-decide critical section. The Lock approach is simpler and correct — we recommend it. The lock is held only for two dict reads / one append / one set of small queue puts, so contention is negligible.
- **`PermissionRequest` event must NOT be buffered.** It is the message that contains the keyboard the user needs to tap. The branch in `_broadcast_loop` flushes the existing buffer (if any) and sends the permission message immediately — *then* the gate effectively closes for subsequent non-permission events. Sketched above.
- **Manager-side ordering.** `_consume_events` MUST log+publish the `PermissionRequest` BEFORE updating `_telegram_pause_count` (otherwise the broadcast subscriber may receive the event before the pause state is set, causing the permission message itself to be buffered instead of sent). Current shape: `await log.append → await bus.publish → bookkeeping`. The bookkeeping happens AFTER `bus.publish`, but `bus.publish` does `put_nowait` on subscriber queues — the subscriber's `async for` body has not yet run. The `_broadcast_loop`'s check of `is_telegram_paused` will see the *post*-publish state because bookkeeping runs before the loop body next awaits. To make this airtight: in `_consume_events`, set the pause state BEFORE `bus.publish`, but then the `_broadcast_loop` will see the permission message and check pause and conclude paused. Solution: the `_broadcast_loop` has special handling for `PermissionRequest` (flush + send immediately, don't check pause), so the order in `_consume_events` is: bookkeeping first (set pause + capture options), then `log.append`, then `bus.publish`. **Document this ordering as a comment** in `_consume_events`.

  Wait — current code does `log.append → bus.publish → branches`. Architect decision: change the order for `PermissionRequest` only: run the bookkeeping BEFORE the bus publish, so subscribers consult an already-set gate. The log-before-publish ordering for the JSONL log is preserved (`log.append` still runs before `bus.publish`). Concretely:

  ```python
  # Sketch — illustrative.
  seq = await log_obj.append(event)
  if isinstance(event, PermissionRequest):
      self._pending_options[event.request_id] = frozenset(event.options)
      self._pending_permissions[event.request_id] = asyncio.Event()
      self._telegram_pause_count[session_id] = (
          self._telegram_pause_count.get(session_id, 0) + 1
      )
      resume = self._telegram_resume.get(session_id)
      if resume is None:
          resume = asyncio.Event()
          self._telegram_resume[session_id] = resume
      resume.clear()
  await self._bus.publish(
      "session.event",
      {"session_id": session_id, "seq": seq, "event": event},
  )
  ```

  This keeps `log.append` first (durability guarantee), then bookkeeping, then publish. The `_broadcast_loop`'s special-case for `PermissionRequest` means the message itself bypasses the gate.

## Test surface

### `tests/test_bot_permission.py` (new)

- `test_callback_paired_user_calls_send_permission_with_right_args` — fake manager, paired user; tap → `send_permission` awaited once with `(session_id, request_id, "approve")`.
- `test_callback_edits_message_with_username_and_removes_keyboard` — assert `edit_text` called with text containing `"→ approve (by @alice)"` and `reply_markup=None`.
- `test_callback_edits_message_falls_back_to_user_id_when_no_username` — username `None` → suffix uses `@123456`.
- `test_callback_unpaired_user_rejected_no_manager_call` — middleware-bypassed handler call with `is_paired_user=False`; `send_permission` not called; `cb.answer("Not paired.", show_alert=True)`.
- `test_callback_stale_session_id_alerts_no_manager_call` — manager raises `StaleSessionError`; assert `cb.answer("Stale prompt", show_alert=True)` and no edit.
- `test_callback_invalid_choice_rejected` — manager's `is_permission_choice_valid` returns False (forged choice) → "Stale prompt" alert; no `send_permission`.
- `test_callback_malformed_callback_data_rejected` — `cb.data="perm:notauuid:r1:approve"` → "Stale prompt" alert; no manager interaction.
- `test_callback_already_answered_message_edit_swallows_bad_request` — second tap; `edit_text` raises `TelegramBadRequest`; handler still completes (`cb.answer()` called).
- `test_keyboard_buttons_match_options_and_callback_data` — `permission_kb(session_id, "r1", ["approve","skip","abort"])` produces exactly three buttons; `callback_data` strings match the spec verbatim.
- `test_keyboard_unknown_option_uses_capitalized_label` — option `"foo"` produces button text `"Foo"`.

### `tests/test_formatting.py` (modify)

- Update every existing assertion: `event_to_messages(...)` returns `[("text", None)]` (or list of tuples). Mechanical change.
- `test_permission_request_returns_message_with_keyboard_sentinel` — replace the existing `test_permission_request_returns_empty_list`. Assert one message; assert text contains `"Permission requested"`, `tool_name`, and the truncated input; assert the keyboard sentinel is non-`None` and carries `request_id` / `options`.

### `tests/test_broadcast.py` (modify)

- Update existing tests: assertions on `bot.send_message.await_args` now expect `reply_markup=None` for non-permission events.
- New: `test_broadcast_buffers_text_event_during_permission_then_drains_in_order` — publish `PermissionRequest`; assert one `send_message` (the permission message, with keyboard). Publish `AssistantTurn(text)`; assert NO new `send_message`. Call `manager.send_permission(session_id, request_id, "approve")` (drives bookkeeping; we don't need a real subprocess — use a fake manager that exposes `is_telegram_paused` and clears it). Publish another `AssistantTurn(text2)`; assert TWO new `send_message` calls in order: buffered text, then text2. (This satisfies the buffering acceptance criterion.)
- New: `test_broadcast_permission_message_carries_keyboard` — publish `PermissionRequest`; assert `send_message` called once per chat with `reply_markup` not None and an `inline_keyboard` of the right shape.
- New: `test_broadcast_sse_subscriber_not_paused` — subscribe a second `bus.subscribe("session.event")` consumer alongside `_broadcast_loop`; publish `PermissionRequest` then `AssistantTurn`; assert the second subscriber receives BOTH events without delay (proving SSE is independent of the Telegram pause).

### `tests/test_session_manager.py` (modify)

- `test_permission_request_pauses_telegram_and_clears_on_response` — drive a fake claude that emits `PermissionRequest`, then a `text`, then nothing; assert `is_telegram_paused(session_id)` is True after the request, then call `send_permission`, assert False.
- `test_concurrent_permission_requests_count_correctly` — emit two `PermissionRequest`; pause count is 2; respond to one; pause count is 1; respond to second; pause clears.
- `test_is_permission_choice_valid_rejects_forged_choice` — capture options `["approve","skip"]`; `is_permission_choice_valid("r1", "abort")` is False.
- `test_session_teardown_clears_pending_permissions` — emit `PermissionRequest`; `await manager.stop()`; assert internal dicts are clear (use the read-only properties, or make `_pending_options` package-private and assert directly with a `getattr`).

### `tests/fakes/fake_claude.py` (no changes needed)

The existing fake reads JSONL from a script file; tests can pre-bake a JSONL fixture with a `permission_request` line.

## Out of scope

- Per-session permission UI in the web viewer (CCR-013 territory; explicitly listed in the ticket's Out of scope).
- Multi-row keyboards / pagination of >8 options. Current usage is 2–3 options.
- Tracking which paired user answered for audit purposes beyond the inline message edit. (Not in scope; logged via Telegram's edit history.)
- Re-sending the permission keyboard if Telegram delivery fails for one chat. We rely on the per-chat best-effort send already in place; the responder's edited message is still authoritative.
- Removing the `TODO(CCR-009)` comment in `process.py:139-160` requires confirming the Claude Code wire format for `permission_response`. If the developer cannot confirm against a real session during this ticket, leave the encoder as-is and update the comment to `TODO(CCR-019)` so the next ticket inherits it. Do not block the ticket on wire-format archaeology.
- Web SSE side: SSE is unaffected by the pause (that is the whole point). No SSE work in this ticket; CCR-012 will subscribe to the same bus and observe events live.

## Order to land changes

1. Widen `OutboundMessage` in `formatting.py` to the tuple type and update every existing producer to return `(text, None)`. Update `test_formatting.py` and `test_broadcast.py` assertions in the same commit. Run those two test files green.
2. Add `keyboards.py` and the `PermissionRequest` branch in `formatting.py`. Update `test_formatting.py` for the new assertion.
3. Update `_ChatSender.enqueue` / `_run` to take and unpack the tuple. Update `_broadcast_loop` to materialise sentinel keyboards and to thread `manager` through.
4. Add the gating state and methods to `SessionManager` (`_pending_options`, `_pending_permissions`, `_telegram_pause_count`, `_telegram_resume`, `is_telegram_paused`, `is_permission_choice_valid`, `wait_for_resume`, plus the `_consume_events` ordering fix and `send_permission` clearing). Add `tests/test_session_manager.py` cases.
5. Add the buffer-and-drain logic in `_broadcast_loop`. Add `tests/test_broadcast.py` buffering test.
6. Add `permission.py` handler + router include in `app.py`. Add `tests/test_bot_permission.py`.
7. Run `pytest`, `ruff check`, `mypy` end-to-end.

The order keeps each commit (or sub-step within one commit, since the workflow lands one commit per ticket) test-green at each boundary. The widen-the-tuple step is the riskiest because it cascades; do it first and confirm green before adding new behavior on top.

## Open questions for team lead

None. The brief's five questions are all answered above:

- `OutboundMessage` widens to `tuple[str, InlineKeyboardMarkup | None]`; `_ChatSender.enqueue` takes the tuple; `_run` passes `reply_markup=kb`.
- Drain via `_broadcast_loop` polling `is_telegram_paused` (no `await wait_for_resume` in the loop body).
- `pending_permissions` keyed by `request_id`; `_telegram_pause_count` keyed by `session_id`; `is_telegram_paused(session_id)` reads the counter.
- Allowed-options source of truth: `SessionManager._pending_options[request_id]` captured from the `PermissionRequest` event in `_consume_events`.
- `_broadcast_loop` owns the buffer; `_ChatSender` is unchanged in lifecycle.

The only soft caveat — the `process.py:139-160` `TODO(CCR-009)` — is documented in "Out of scope" with a clear escape hatch (defer to CCR-019 if the wire format cannot be confirmed end-to-end during dev work). Team lead should confirm that defer is acceptable; if not, the ticket gains a smoke-test step against a real `claude` binary.
