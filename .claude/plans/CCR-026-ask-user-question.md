# Plan: CCR-026 — AskUserQuestion handler

## Goal

Recognise Claude's built-in `AskUserQuestion` tool as a first-class
interactive surface in Telegram. When an `AssistantTurn` carries a
`tool_use` block whose `name == "AskUserQuestion"`, the bot broadcasts the
question (with an inline keyboard if discrete `options` are present, or a
typed-reply prompt otherwise) to every paired chat. Any paired user can
answer (button tap or typed reply); the answer is fed back to the running
Claude session as a synthetic `tool_result` content block via a new
`SessionManager.send_tool_result(...)` API. Stale, unknown, and timed-out
replies fail closed without crashing the session. After this ticket,
`AskUserQuestion` invocations from a real `claude -p` session round-trip
through Telegram with no MCP involvement (orthogonal channel from CCR-025).

## File layout

- `src/ccr/claude/manager.py` — **modify** — track outstanding
  `AskUserQuestion` `tool_use_id`s in a new `_pending_questions` dict;
  detect `AskUserQuestion` blocks inside `_consume_events`'s
  `AssistantTurn` walk (alongside the existing `_track_subagents` call);
  add public `async send_tool_result(tool_use_id, content, *,
  is_error=False) -> bool`; cancel-on-teardown analogous to
  `_mcp.cancel_pending` in `_teardown_locked`. New errors:
  `StaleToolUseError` (subclass of `SessionError`) raised by
  `send_tool_result` when `tool_use_id` is not registered. Returns `bool`
  for "did THIS call deliver"; same shape as
  `resolve_permission` so the bot maps `False` to "Stale prompt".
- `src/ccr/claude/process.py` — **no signature change** — `send_user_turn`
  already accepts `list[ContentBlock]`. The manager builds a
  `[ToolResultBlock(...)]` and reuses the existing wire path. Document this
  reuse in `send_tool_result`'s docstring; do **not** add a parallel
  `send_tool_result` on `ClaudeProcess`.
- `src/ccr/bot/formatting.py` — **modify** — extend `_format_assistant_turn`
  (or insert an upstream branch in `event_to_messages`) to detect
  `ToolUseBlock` with `name == "AskUserQuestion"` BEFORE the generic
  `_format_tool_use` branch fires. Render `(text, _PendingKeyboard(...))`
  when `tool_input` carries a non-empty `options` list, or
  `(text, None)` for the free-text case. Extend `_PendingKeyboard` with a
  `kind: Literal["perm", "auq"]` discriminator (default `"perm"` for
  back-compat) so `_materialise_keyboards` can dispatch to the right
  keyboard builder.
- `src/ccr/bot/keyboards.py` — **modify** — add
  `ask_user_question_kb(session_id, question_id, options) -> InlineKeyboardMarkup`
  building one button per option with `callback_data="auq:{session_id}:{question_id}:{idx}"`
  (option **index**, not text — keeps callback_data well under 64 bytes
  even with long option strings). Existing `permission_kb` and `_LABELS`
  map untouched.
- `src/ccr/bot/handlers/ask_user_question.py` — **create** — new aiogram
  Router covering three entry points:
  - `cb_ask_user_question` — `F.data.startswith("auq:")` callback handler:
    parse the triple, look up option text by index from a manager-side
    helper, call `session_manager.send_tool_result(tool_use_id, option_text)`,
    edit the original message with the chosen option suffix (mirrors
    `cb_permission`).
  - `cmd_answer` — `Command("answer")`: `/answer <id8> <text>` always
    works (multi-question safe); rejects malformed args without touching
    the manager.
  - `handle_free_text_answer` — plain-text handler claimed by a custom
    filter that returns `True` only when **exactly one** free-text
    question is outstanding for the live session. Otherwise the filter
    fails so the message falls through to the existing
    `session.handle_text` handler.
- `src/ccr/bot/app.py` — **modify** — register the new router **before**
  `session_router` so the conditional plain-text-claim filter resolves
  before `session.handle_text`'s `F.text & ~F.text.startswith("/")` does.
  Order becomes: pairing → ask_user_question → session → permission →
  passthrough.
- `src/ccr/server.py` — **modify** — extend `_materialise_keyboards` to
  branch on `slot.kind`: `"perm"` → `permission_kb(...)` (existing),
  `"auq"` → `ask_user_question_kb(...)`. No new bus topics, no
  pause/buffer logic.
- `src/ccr/config.py` — **modify** — add
  `ask_user_question_timeout_seconds: int = 600` with a
  `field_validator` enforcing `1 <= value <= 3600`. Mirrors the
  `mcp_permission_timeout_seconds` precedent. 600 s default (longer than
  MCP's 120 s — a free-text answer often needs more thought than a
  permission tap).
- `src/ccr/claude/manager.py` (continued) — when registering a question,
  schedule an `asyncio.Task` that fires after the timeout and calls
  `send_tool_result(tool_use_id, "...", is_error=True)` if the question
  is still outstanding. The task is stored on the `_PendingQuestion`
  record so `_teardown_locked` and `send_tool_result` itself can cancel
  it.
- `tests/test_bot_ask_user_question.py` — **create** — see "Test surface".
- `tests/test_session_manager.py` — **modify** — extend with
  `send_tool_result` happy-path / stale / timeout / teardown cases.
- `tests/test_formatting.py` — **modify** — render an `AssistantTurn`
  carrying an `AskUserQuestion` `tool_use` block (with options /
  without options); assert the right text + keyboard sentinel.
- `tests/test_broadcast.py` — **modify** — assert the auq-kind
  `_PendingKeyboard` sentinel is materialised via
  `ask_user_question_kb` (not `permission_kb`).
- `tests/fakes/fake_claude.py` — **no change**. The fake's existing
  `FAKE_CLAUDE_SCRIPT` env-driven JSONL replay is enough to inject an
  `AskUserQuestion` `tool_use` block into a session.

## Public surface

### `src/ccr/claude/manager.py` — added surface

```python
# Sketch — illustrative, not the final code.

class StaleToolUseError(SessionError):
    """Raised when send_tool_result is called for an unknown tool_use_id."""


@dataclass(slots=True)
class _PendingQuestion:
    tool_use_id: str
    session_id: uuid.UUID
    options: list[str]            # empty for free-text questions
    timeout_task: asyncio.Task[None] | None


class SessionManager:
    def __init__(self, *, bus, db_factory, settings) -> None:
        ...
        # AskUserQuestion bookkeeping (CCR-026). Lifetime = session
        # lifetime; cleared in _teardown_locked.
        self._pending_questions: dict[str, _PendingQuestion] = {}

    async def send_tool_result(
        self,
        tool_use_id: str,
        content: str,
        *,
        is_error: bool = False,
    ) -> bool:
        """Feed a synthetic tool_result back to the running Claude session.

        Returns ``True`` if the call delivered (the tool_use_id was
        registered). Returns ``False`` if the id is unknown OR the
        session has changed since the question was published. The bot
        maps ``False`` to "Stale prompt".

        Raises ``NoActiveSessionError`` when no Claude subprocess is held
        — same precondition as :meth:`send`.
        """

    def is_question_pending(self, tool_use_id: str) -> bool:
        """Return ``True`` iff a question is registered and unresolved."""

    def question_options(self, tool_use_id: str) -> list[str] | None:
        """Return the option list carried by the question, or ``None``
        if the id is unknown. Used by the bot's callback handler to
        translate a button-index callback into the option text that
        goes back to claude as the tool_result."""

    def outstanding_free_text_questions(self) -> list[str]:
        """Return tool_use_ids of every outstanding question with no
        ``options`` (empty list). Used by the plain-text filter to
        decide whether to claim the update."""

    def question_id_by_prefix(self, id_prefix: str) -> str | None:
        """Resolve an 8-hex-prefix to the full tool_use_id of an
        outstanding question. Returns ``None`` if zero or multiple
        match. Used by ``/answer <id8> <text>``."""
```

Implementation notes for `send_tool_result` (in plan, not in sketch):
- Delivery uses `await self._proc.send_user_turn([ToolResultBlock(
  type="tool_result", tool_use_id=tool_use_id,
  content=content, is_error=is_error)])`.
- BEFORE awaiting, atomically pop the entry from `_pending_questions`
  AND cancel its timeout task. If the entry was already gone, return
  `False`.
- Exception path: if `proc.send_user_turn` raises `RuntimeError`
  (stdin closed mid-flight), log a structured WARN and return `False`
  — the question is gone but we cannot deliver; treat as if the
  session crashed.

### `src/ccr/bot/keyboards.py` — added builder

```python
# Sketch — illustrative.
def ask_user_question_kb(
    session_id: uuid.UUID,
    question_id: str,
    options: list[str],
) -> InlineKeyboardMarkup:
    """Build the inline keyboard for an AskUserQuestion prompt.

    callback_data is ``auq:{session_id}:{question_id}:{idx}`` — option
    INDEX, not text, so callback_data stays under Telegram's 64-byte cap
    even with long options (e.g. multi-word labels). The handler resolves
    idx → option text via SessionManager.question_options.
    """
```

### `src/ccr/bot/formatting.py` — extended branch

```python
# Sketch — illustrative.
class _PendingKeyboard(NamedTuple):
    kind: Literal["perm", "auq"]  # NEW field; widen back-compat below.
    request_id: str               # tool_use_id for AUQ; request_id for perm.
    options: list[str]


def _format_ask_user_question(block: ToolUseBlock) -> OutboundMessage:
    """Render an AskUserQuestion tool_use block into one outbound message.

    ``tool_input`` shape (best-effort; see "Edge cases" / "Open questions"
    for the wire-format caveat):
    - ``question`` (str) — the text shown to the user.
    - ``options`` (list[str]) — discrete choices; absent or empty means
      free-text reply.
    - other keys are tolerated and ignored.
    """
    raw = block.input
    question = html.escape(str(raw.get("question", "")))
    options_raw = raw.get("options") or []
    options = [str(o) for o in options_raw if isinstance(options_raw, list)]
    text = f"❓ {question}"
    if not options:
        id8 = block.id[:8]
        text += (
            f"\nReply with text, or /answer <code>{id8}</code> &lt;text&gt;."
        )
        return (text, None)
    return (text, _PendingKeyboard(kind="auq",
                                   request_id=block.id,
                                   options=list(options)))
```

`event_to_messages` keeps its current dispatch — the `AskUserQuestion`
detection lives inside `_format_assistant_turn` so the existing per-turn
ordering (text, then thinking, then tool-uses) is preserved. The
`AskUserQuestion` block replaces the generic
`_format_tool_use(block)` line; other tool_use blocks in the same
turn render as today.

### `src/ccr/server.py` — extended materialiser

```python
# Sketch — illustrative.
def _materialise_keyboards(messages, session_id):
    out: list[OutboundMessage] = []
    for text, slot in messages:
        if isinstance(slot, _PendingKeyboard):
            if slot.kind == "auq":
                kb = ask_user_question_kb(session_id, slot.request_id, slot.options)
            else:  # "perm" — default
                kb = permission_kb(session_id, slot.request_id, slot.options)
            out.append((text, kb))
        else:
            out.append((text, slot))
    return out
```

### `src/ccr/bot/handlers/ask_user_question.py` — new handler

```python
# Sketch — illustrative.
router = Router(name="ask_user_question")

_AUQ_PREFIX = "auq:"
_AUQ_PART_COUNT = 4


def _parse_auq_callback(data: str) -> tuple[uuid.UUID, str, int] | None:
    if not data.startswith(_AUQ_PREFIX):
        return None
    parts = data.split(":", 3)
    if len(parts) != _AUQ_PART_COUNT:
        return None
    _, sid_str, tool_use_id, idx_str = parts
    try:
        return uuid.UUID(sid_str), tool_use_id, int(idx_str)
    except ValueError:
        return None


@router.callback_query(F.data.startswith(_AUQ_PREFIX))
async def cb_ask_user_question(
    cb: CallbackQuery,
    session_manager: SessionManager,
    is_paired_user: bool,
) -> None:
    if not is_paired_user:
        await cb.answer("Not paired.", show_alert=True)
        return
    parsed = _parse_auq_callback(cb.data or "")
    if parsed is None:
        await cb.answer("Stale prompt", show_alert=True)
        return
    _session_id, tool_use_id, idx = parsed
    options = session_manager.question_options(tool_use_id)
    if options is None or not 0 <= idx < len(options):
        await cb.answer("Stale prompt", show_alert=True)
        return
    answer_text = options[idx]
    delivered = await session_manager.send_tool_result(tool_use_id, answer_text)
    if not delivered:
        await cb.answer("Stale prompt", show_alert=True)
        return
    # Edit-with-suffix mirrors cb_permission's UX.
    username = cb.from_user.username or str(cb.from_user.id)
    suffix = f"\n→ {html.escape(answer_text)} (by @{html.escape(username)})"
    if cb.message is not None:
        original = cb.message.html_text or cb.message.text or ""
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.edit_text(original + suffix, reply_markup=None)
    await cb.answer()


@router.message(Command("answer"))
async def cmd_answer(
    msg: Message,
    session_manager: SessionManager,
) -> None:
    """`/answer <id8> <text>` — direct typed reply to a specific question."""
    raw = (msg.text or "").removeprefix("/answer").strip()
    parts = raw.split(maxsplit=1)
    if len(parts) != 2 or not _HEX8_RE.match(parts[0]):
        await msg.answer("Usage: /answer <8-hex-id> <text>")
        return
    id_prefix, text = parts
    full_id = session_manager.question_id_by_prefix(id_prefix)
    if full_id is None:
        await msg.answer("Stale prompt")
        return
    delivered = await session_manager.send_tool_result(full_id, text)
    if not delivered:
        await msg.answer("Stale prompt")
        return
    await msg.answer("Forwarded.")


# Custom filter — claim plain text only when exactly ONE free-text
# question is outstanding. aiogram passes workflow data as kwargs to
# filter callables, so session_manager is available here.
async def _single_free_text_outstanding(
    message: Message,
    session_manager: SessionManager,
) -> bool:
    if message.text is None or message.text.startswith("/"):
        return False
    return len(session_manager.outstanding_free_text_questions()) == 1


@router.message(F.text & ~F.text.startswith("/"), _single_free_text_outstanding)
async def handle_free_text_answer(
    msg: Message,
    session_manager: SessionManager,
) -> None:
    """Plain text → answer the single outstanding free-text question."""
    pending = session_manager.outstanding_free_text_questions()
    # Filter guarantees len == 1, but re-check defensively (race with
    # another paired user's answer).
    if len(pending) != 1:
        return  # let session.handle_text pick it up next time
    delivered = await session_manager.send_tool_result(pending[0], msg.text or "")
    if not delivered:
        await msg.answer("Stale prompt")
        return
    await msg.answer("Forwarded.")
```

### Settings addition

```python
ask_user_question_timeout_seconds: int = 600

@field_validator("ask_user_question_timeout_seconds")
@classmethod
def _ask_timeout_in_range(cls, value: int) -> int:
    if not MCP_TIMEOUT_MIN <= value <= MCP_TIMEOUT_MAX:
        raise ValueError(...)
    return value
```

Reuse the existing `MCP_TIMEOUT_MIN`/`MCP_TIMEOUT_MAX` constants
(1–3600) — the bound applies; renaming is unnecessary churn.

## Patterns and prior art

- **Reuse — `_PendingKeyboard` sentinel + `_materialise_keyboards`:**
  `src/ccr/bot/formatting.py:48-65` and `src/ccr/server.py:131-148` are
  the formatter-purity pattern from CCR-009/CCR-025. Extend with a `kind`
  discriminator so the second consumer (auq) can dispatch alongside
  perm.
- **Reuse — `permission_kb` shape:** `src/ccr/bot/keyboards.py:33-59`.
  `ask_user_question_kb` mirrors it 1:1; the only differences are the
  callback prefix (`auq:` vs `perm:`) and that the option *index* goes
  in callback_data instead of the option text (we cannot guarantee
  AskUserQuestion options stay short).
- **Reuse — `cb_permission` handler shape:** `src/ccr/bot/handlers/permission.py`
  is the canonical edit-with-suffix + stale-handling pattern. The new
  `cb_ask_user_question` mirrors it line-for-line including
  `is_paired_user` short-circuit, malformed-data handling,
  `contextlib.suppress(TelegramBadRequest)` around the edit, and
  `cb.answer()` without alert on the success path.
- **Reuse — `MCP permission server` resolve-via-Future pattern:**
  CCR-025's `McpPermissionServer.resolve` returns a bool (`True` if THIS
  call set the result; `False` if unknown / already resolved). The bot
  maps `False` to "Stale prompt". `SessionManager.send_tool_result`
  follows the same contract — same return shape, same canned message,
  same precedent for fail-closed cancellation on teardown.
- **Reuse — `_track_subagents` walk in `_consume_events`:**
  `src/ccr/claude/manager.py:587-606` already iterates
  `AssistantTurn.message.content` looking for tool_use blocks by name.
  Add a parallel walk (or extend the same walk) for
  `name == "AskUserQuestion"`. **Do not fold AskUserQuestion tracking
  into `_track_subagents`** — they have separate purposes; future
  changes to one shouldn't drag the other.
- **Reuse — slim DB writes / no-DB invariant from `CLAUDE.md`:**
  `_pending_questions` is in-memory only. No SQL schema change. Same
  precedent as `_running_subagents`, `_skills`,
  `_rate_limit_status`, and CCR-025's MCP Future map.
- **Reuse — `send_user_turn([ToolResultBlock(...)])`:**
  `src/ccr/claude/process.py:154-174` already serialises a
  `list[ContentBlock]` into the documented `tool_result` wire format
  via `model_dump(mode="json")`. The manager builds a `ToolResultBlock`
  and hands it off — no new low-level write path.
- **Reuse — `_HEX8_RE` regex pattern:** `src/ccr/bot/handlers/session.py:46`
  already has `_HEX8_RE = re.compile(r"^[0-9a-f]{8}$")`. Either lift it
  to a shared module (e.g. `src/ccr/bot/_short_id.py`) or duplicate the
  one-liner in `ask_user_question.py`. Per "no premature
  abstractions" — duplicate. Three callers (session.py, ask_user_question.py,
  and one more) would justify lifting; we have two.
- **Reuse — router registration discipline:** `src/ccr/bot/app.py:53-56`.
  Routers are resolved in order; the new router slots BETWEEN
  pairing_router and session_router so the conditional plain-text claim
  fires before `session.handle_text`'s catch-all.
- **Avoid — a new "TypedReplyServer" class:** there is no async,
  long-lived state to encapsulate. `_pending_questions` is a dict on
  `SessionManager`, registered in `_consume_events`, drained in
  `send_tool_result` / `_teardown_locked`. Hoisting to a class would
  duplicate `McpPermissionServer`-style boilerplate for two dicts and
  a per-question timeout task — not worth it.
- **Avoid — building a synthetic bus envelope (like `McpPermissionRequest`).**
  AskUserQuestion arrives as a real JSONL `tool_use` block inside an
  `AssistantTurn`; it IS already a `ClaudeEvent`. The formatter detects
  it directly. No new envelope needed.
- **Avoid — adding `send_tool_result` on `ClaudeProcess`.** The wire
  shape (`{"type":"user","message":...,"content":[{"type":"tool_result",...}]}`)
  is exactly what `send_user_turn(content: list[ContentBlock])` already
  emits. Adding a parallel low-level method would duplicate
  `_write_line` consumers. The manager's outstanding-id bookkeeping is
  the only thing wrapping this — that's session-level, not
  process-level.
- **Avoid — re-introducing the broadcast pause/buffer.** CCR-025
  deleted it; concurrent AskUserQuestion calls do NOT need it (each
  question has its own keyboard / its own id). Same architectural
  decision.
- **Avoid — accepting plain text whenever ANY free-text question is
  outstanding.** "Last open question wins" is fragile (which one is
  "last"? — order of arrival? order of broadcast? the one the user
  saw first?). The plain-text filter requires **exactly one**
  outstanding; otherwise the user must use `/answer <id8>`. Two paths
  but each one has an obvious mental model.

## Abstractions

**No new long-lived class.** AskUserQuestion bookkeeping is two dicts
and a timeout task per question — same shape as
`_running_subagents` / `_skills` / `_rate_limit_status` already on
`SessionManager`. The pattern of "register on event, drain on reply
or teardown" is established. A `_PendingQuestion` `@dataclass` carries
the per-question state (`tool_use_id`, `session_id`, `options`,
`timeout_task`) — a dataclass, not a class with methods, because the
state is data and the operations live on `SessionManager`.

**Extend `_PendingKeyboard`, do not duplicate it.** A second sentinel
class (`_PendingAuqKeyboard`) would force `_materialise_keyboards` and
its caller to maintain two parallel branches by isinstance. Adding a
`kind` discriminator collapses to one branch with a tag dispatch and
keeps the shape symmetric.

**No `bot.kind` switch elsewhere.** `_materialise_keyboards` is the one
place where `kind` matters; everywhere else (formatter, sender queue,
permission handler) the sentinel is opaque.

## Dependencies

- **Depends on:**
  - `src/ccr/claude/manager.py` — `SessionManager` instance + `_consume_events` already wired.
  - `src/ccr/claude/process.py` — `send_user_turn` accepts `list[ContentBlock]` (CCR-007).
  - `src/ccr/claude/events.py` — `ToolUseBlock`, `ToolResultBlock` (existing).
  - `src/ccr/bot/keyboards.py` — `permission_kb` not modified, just sibling added.
  - `src/ccr/bot/formatting.py` — `_PendingKeyboard` widened with `kind`.
  - `src/ccr/server.py` — `_materialise_keyboards` extended.
  - `src/ccr/config.py` — `ask_user_question_timeout_seconds` field.
  - CCR-025 (only at the formatting level — they share the
    `_PendingKeyboard` sentinel; functionally orthogonal).
- **Used by:**
  - Future CCR-027 (Plan-mode UX) — reuses the `/answer` /
    button-tap surface verbatim if the plan-mode acknowledgment is
    surfaced as an `AskUserQuestion` (per CCR-027 Notes; whether
    plan-mode actually emits `AskUserQuestion` or a different
    structured event is a separate Step-0 probe in CCR-027 territory,
    not this ticket).

## Edge cases the developer must handle

- **`AskUserQuestion` `tool_input` shape is unprobed.** The ticket
  guesses `{"question": str, "options": [str, ...]}` based on Claude's
  publicly documented API and existing prior art. The repo's session
  logs only show `AskUserQuestion` listed in `tools` arrays, never as a
  real `tool_use` block. **Step-0 probe required before implementation:**
  the developer runs a real `claude -p --input-format=stream-json
  --output-format=stream-json --verbose` session with a prompt that
  forces `AskUserQuestion` (e.g. "ask me what color my car is using
  AskUserQuestion") and captures the JSONL line, exactly as CCR-021
  prescribed for the permission-event probe. Document the actual
  `tool_input` schema in the developer's report. Three outcomes:
  1. Matches the guessed shape → implement as planned.
  2. Different shape (e.g. `prompt` instead of `question`, options
     under a different key, options as `[{"value": "x", "label": "X"}]`
     instead of plain strings) → adjust `_format_ask_user_question`
     and `question_options` accordingly; flag the deviation in the
     dev report.
  3. `AskUserQuestion` does not surface as a normal `tool_use` block
     (e.g. claude routes it through MCP or a different stream entirely)
     → return `BLOCKED` with the probe transcript; do **not** improvise.
- **Free-text reply correlation when zero outstanding.** Filter
  returns `False`; aiogram falls through to `session.handle_text`.
  The user's message becomes a normal user-turn forward.
- **Free-text reply correlation when ≥2 outstanding.** Filter returns
  `False`. The user's message lands in `session.handle_text`. The
  bot does NOT auto-route to either question, and the user must use
  `/answer <id8> <text>` to disambiguate. The free-text question
  broadcasts include the `<id8>` and the `/answer` hint so this is
  discoverable.
- **Concurrent button taps on the same `AskUserQuestion` keyboard.**
  Same as CCR-025: `_pending_questions.pop(tool_use_id)` is the
  single-shot resolution. The first tap pops; the second sees `None`
  and `send_tool_result` returns `False` → "Stale prompt".
- **Race between button tap and `/answer` for the same question.**
  Same single-shot guarantee — whichever atomic pop wins, the loser
  gets `False`.
- **Race between Telegram answer and timeout.** The timeout task
  attempts `send_tool_result(..., is_error=True)`; if the user's
  answer popped first, the timeout sees `False` and silently exits.
- **Manager teardown while questions are outstanding.**
  `_teardown_locked` MUST iterate `_pending_questions`, cancel each
  `timeout_task`, and clear the dict — analogous to
  `_mcp.cancel_pending(session_id)` at `src/ccr/claude/manager.py:511`.
  No `tool_result` is sent back on teardown (the subprocess is being
  torn down anyway; the unanswered question dies with it). Document
  this in `_teardown_locked`'s body.
- **Question registered against a stale subprocess.** If the
  AssistantTurn arrives during a session and the session is replaced
  before the user answers, `_teardown_locked` clears the dict — the
  user's button tap on the old keyboard maps to "Stale prompt" via
  the unknown-id path. The button keyboard is NOT proactively
  edited to "session ended" (out of scope; same precedent as
  CCR-025's timeout-message-edit deferral).
- **Timeout policy.** Default 600 s (10 minutes), configurable via
  `ask_user_question_timeout_seconds` (1–3600 range, validator
  rejects out-of-range). On timeout, post a `tool_result` with
  `is_error=True` and `content=f"Timed out — no paired user
  responded within {N}s"`. Log a structured WARN. The Telegram
  message is NOT proactively edited (same deferral as CCR-025's
  timeout-message-edit).
- **Stale-reply guard via `send_tool_result` returning `False`.** Bot
  always renders the same canned `"Stale prompt"` alert (button) or
  `"Stale prompt"` message reply (`/answer`) on `False`. Same wording
  as `cb_permission` for consistency.
- **`tool_use_id` stays opaque.** It is supplied by Claude in the
  `tool_use` block; the bot treats it as a string. Only the prefix
  (`tool_use_id[:8]`) is shown to users (in the broadcast text and as
  the `/answer` argument). The full id round-trips through
  `callback_data` for the button path, and `question_id_by_prefix`
  resolves prefix → full id for the `/answer` path.
- **Prefix collisions.** Two outstanding questions whose `tool_use_id`
  share the same 8-hex prefix: `question_id_by_prefix` returns
  `None` (the same way `SessionManager.continue_session_by_prefix`
  treats collisions — see `src/ccr/claude/manager.py:786-797` for the
  in-Python disambiguation pattern). The bot replies "Stale prompt"
  with a hint that the user should use a longer prefix; the user
  can answer via the button instead. Vanishingly rare in practice
  (random `tool_use_id` is 24 chars+, so 8-hex prefix collision is
  effectively zero for two outstanding questions); call this out
  with a one-line WARN log if it ever fires.
- **HTML escaping of question text and option labels.** All
  user-visible content goes through `html.escape` before
  interpolation — same precedent as
  `_format_tool_use` / `_format_tool_result_error` /
  `_format_mcp_permission`. Question text containing `<script>` or
  `&` must not break the parse.
- **Empty question text or empty options list.** A `tool_use`
  carrying `tool_input == {}` should still render — fall back to
  `"❓ (no question text)"` and free-text prompt. An options
  list with one entry renders as a single-button keyboard
  (degenerate but valid). Empty list ⇒ free-text path.
- **`callback_data` length budget.** Worst case:
  `auq:` (4) + UUID4 (36) + `:` (1) + tool_use_id (Claude uses
  24-char `toolu_*` prefixes in practice) + `:` (1) +
  index-up-to-2-digits (2) = ~68 bytes — JUST over the 64-byte cap.
  **Use `tool_use_id[:8]` as the callback's question id** (same
  short-id we already show users), with the full id stored in
  `_pending_questions[full_id]`. The handler resolves
  prefix → full id via the same `question_id_by_prefix` helper.
  Total callback now: 4 + 36 + 1 + 8 + 1 + 2 = 52 bytes. Safe.
- **Plain-text from an unpaired sender.** Already short-circuited by
  `AllowlistMiddleware` before any router runs; no extra guard
  needed in the new handler. The middleware's
  `_REJECTION` ("Not paired. ...") fires before our filter sees the
  message.
- **`/answer` from an unpaired sender.** Same — middleware blocks.
  Defence in depth: `cmd_answer` could check `is_paired_user` from
  workflow data, but per existing handlers (`/new`, `/stop`, etc.)
  this is omitted because the middleware is trusted.

## Test surface

### `tests/test_bot_ask_user_question.py` (new)

Coverage targets — golden, failure, edge:

- `test_format_ask_user_question_with_options_returns_keyboard_sentinel`
  — formatter sees an `AssistantTurn` carrying `ToolUseBlock(name="AskUserQuestion", id="toolu_abc...", input={"question": "Pick one", "options": ["A", "B"]})`; one outbound message; sentinel is `_PendingKeyboard(kind="auq", request_id="abc..."[:8], options=["A", "B"])`.
- `test_format_ask_user_question_without_options_returns_free_text_prompt`
  — no `options`; outbound text includes the question and the `/answer <id8>` hint; sentinel slot is `None`.
- `test_format_ask_user_question_html_escapes_question_and_options`
  — question text `"<script>"`, option `"A & B"` → both escaped in
  outbound text / button labels.
- `test_format_ask_user_question_empty_input_falls_back`
  — `tool_input={}` → outbound text is the canned fallback.
- `test_broadcast_renders_auq_with_ask_user_question_kb`
  — publish an `AssistantTurn` envelope to the bus; assert
  `bot.send_message` called with `reply_markup` an
  `InlineKeyboardMarkup` whose `callback_data` starts with `"auq:"`
  (NOT `"perm:"`).
- `test_cb_ask_user_question_button_tap_sends_tool_result_with_chosen_text`
  — fake manager; tap idx 0 → `send_tool_result` awaited with
  `tool_use_id` and content `"A"`; message edited with the
  `→ A (by @user)` suffix; `cb.answer()` called.
- `test_cb_ask_user_question_unpaired_user_rejected`
  — `is_paired_user=False`; `send_tool_result` not called;
  `cb.answer("Not paired.", show_alert=True)`.
- `test_cb_ask_user_question_malformed_callback_rejected`
  — `cb.data="auq:notauuid:tu_id:0"` → "Stale prompt"; no manager
  interaction.
- `test_cb_ask_user_question_index_out_of_range_rejected`
  — `manager.question_options` returns 2 options, callback carries
  idx 5 → "Stale prompt"; no `send_tool_result` call.
- `test_cb_ask_user_question_unknown_tool_use_id_rejected`
  — `manager.question_options` returns `None` → "Stale prompt"; no
  `send_tool_result` call.
- `test_cb_ask_user_question_send_tool_result_returns_false_alerts_no_edit`
  — `send_tool_result` returns `False` (already answered) →
  "Stale prompt", no `edit_text`.
- `test_cb_ask_user_question_concurrent_taps_only_first_succeeds`
  — first tap pops the question; second tap sees `None` from
  `question_options` (or `False` from `send_tool_result`) → "Stale
  prompt".
- `test_cmd_answer_routes_to_correct_tool_use_id`
  — register two questions with full ids `tu_aaaaaaaa...` and
  `tu_bbbbbbbb...`; `/answer aaaaaaaa hello` → `send_tool_result`
  awaited with the first id and content `"hello"`; reply `Forwarded.`.
- `test_cmd_answer_malformed_args_rejected`
  — `/answer foo` (no body) → usage hint; `/answer XYZ text` (not 8
  hex) → usage hint; no manager interaction.
- `test_cmd_answer_unknown_prefix_rejected`
  — `/answer 99999999 hello` (no question matches) →
  "Stale prompt"; no `send_tool_result` call.
- `test_cmd_answer_ambiguous_prefix_rejected`
  — `_question_id_by_prefix` returns `None` for a colliding prefix
  → "Stale prompt".
- `test_handle_free_text_answer_routes_to_single_outstanding_question`
  — register exactly one free-text question; user types "blue" →
  `send_tool_result(tool_use_id, "blue")`; reply `Forwarded.`.
- `test_handle_free_text_answer_filter_falls_through_when_zero_outstanding`
  — no outstanding free-text questions; user types "blue" →
  the new handler does NOT match (filter returns False) so
  `session.handle_text` claims the message and forwards as a
  user-turn. Test by mocking both `session_manager.send` and
  `session_manager.send_tool_result` and asserting only `send` was
  called.
- `test_handle_free_text_answer_filter_falls_through_when_two_outstanding`
  — two free-text questions outstanding → filter False → falls
  through to `session.handle_text`.
- `test_handle_free_text_answer_filter_excludes_slash_prefix`
  — even with one outstanding question, `/cost` (slash command)
  routes through the passthrough router, not our handler.

### `tests/test_session_manager.py` (modify)

Append:

- `test_ask_user_question_block_registers_pending_question`
  — fake claude emits an `AssistantTurn` with one
  `AskUserQuestion` `tool_use` block; after the consumer drains,
  `manager.is_question_pending(tool_use_id) is True` and
  `manager.outstanding_free_text_questions()` reflects the right
  ids.
- `test_send_tool_result_happy_path`
  — register a question (via fake claude or directly via internal
  helper); call `manager.send_tool_result(tool_use_id, "hello")`;
  assert `True`; assert the proc's stdin received a `user`-turn
  JSONL with one `tool_result` content block whose
  `tool_use_id` and `content` match.
- `test_send_tool_result_unknown_id_returns_false`
  — `manager.send_tool_result("nope", "x")` returns `False`; no
  stdin write.
- `test_send_tool_result_already_answered_returns_false`
  — answer once; call again with same id → `False`.
- `test_send_tool_result_no_active_session_raises`
  — manager is idle (`_proc is None`); `send_tool_result` raises
  `NoActiveSessionError`.
- `test_ask_user_question_timeout_sends_error_tool_result`
  — set `ask_user_question_timeout_seconds=0.05` (override via test
  Settings); register a question; wait for the timeout task to
  fire; assert the proc received a `tool_result` with
  `is_error=True` and a content message containing "Timed out".
- `test_teardown_clears_pending_questions_and_cancels_timeouts`
  — register a question; call `manager.stop()`; assert
  `is_question_pending(tool_use_id) is False` and the timeout task
  was cancelled (no `tool_result` written to stdin during teardown).
- `test_question_id_by_prefix_resolves_unique_prefix`
  — register `tu_ffeebbaa...`; `question_id_by_prefix("ffeebbaa")`
  returns the full id.
- `test_question_id_by_prefix_returns_none_on_collision`
  — register two ids whose 8-hex prefixes collide (constructed
  manually); `question_id_by_prefix(prefix)` returns `None`.
- `test_question_options_unknown_returns_none`
  — `question_options("nope")` returns `None`.

### `tests/test_formatting.py` (modify)

- `test_assistant_turn_with_ask_user_question_options_renders_keyboard_sentinel`
  — described above.
- `test_assistant_turn_with_ask_user_question_no_options_renders_text_only`
  — described above.
- `test_assistant_turn_with_mixed_blocks_keeps_other_tool_uses_unchanged`
  — `AssistantTurn` containing TextBlock + AskUserQuestion + a
  generic `Bash` ToolUseBlock → outbound list has 3 entries in
  order: text, the auq message with sentinel, the `🔧 Bash ...`
  one-liner.

### `tests/test_broadcast.py` (modify)

- `test_broadcast_renders_auq_keyboard_via_ask_user_question_kb`
  — publish an `AssistantTurn` carrying an `AskUserQuestion`
  block with options; assert `bot.send_message` reply_markup is an
  `InlineKeyboardMarkup` whose buttons' `callback_data` starts with
  `"auq:"` and whose count matches the option list.

### `tests/test_session_manager.py` — coverage gate

The whole `pytest --cov=ccr --cov-fail-under=80` gate must stay green;
the new module / handler additions push line counts up, so the test
surface above is sized to keep coverage well above 80% (CCR-025
landed at ~88% — same target).

## Out of scope

Mirrors the ticket's `Out of scope:` block plus deliberate cuts:

- Plan-mode UX (CCR-027 — separate ticket).
- Per-session question UI in the web viewer (the bus payload already
  carries the AssistantTurn → CCR-013 can render it without any
  change here).
- Web-side answer surface (Telegram-only for now).
- Persisting question history across sessions (in-memory only;
  cleared on every `_teardown_locked`).
- Editing the original Telegram message on timeout to "Timed out —
  please re-prompt" (deferred follow-up; same trade-off as CCR-025).
- Allowing the user to **edit** their typed answer before sending
  (Telegram does not have a native UX for this; out of scope).
- A "cancel question" button on the keyboard. The button-only path
  has only the listed options + nothing else; the user can wait for
  timeout or `/stop` the session. (If we add it later, callback_data
  uses idx `-1` or a sentinel string.)
- Generic "AskUserQuestion server" abstraction. One concrete consumer
  in this ticket; CCR-027 may or may not reuse it; do not pre-empt.

## Order to land changes

1. Add `_PendingQuestion` dataclass + the `_pending_questions`
   dict + `question_options` / `question_id_by_prefix` /
   `outstanding_free_text_questions` / `is_question_pending`
   accessors on `SessionManager` (no consumers yet). Tests for the
   accessors with manually-seeded state.
2. Wire detection in `_consume_events` — walk
   `AssistantTurn.message.content` for `AskUserQuestion` blocks
   alongside the existing `_track_subagents` call. Schedule the
   timeout task. Test via fake claude script.
3. Add `send_tool_result` to `SessionManager` (uses
   `proc.send_user_turn([ToolResultBlock(...)])`). Test happy/stale/
   no-active-session/timeout/teardown.
4. Extend `_PendingKeyboard` with `kind`. Adjust `_materialise_keyboards`
   to dispatch on `kind`. Update `tests/test_formatting.py` and
   `tests/test_broadcast.py` to cover the new branch (existing perm
   tests must still pass — `kind="perm"` is the default).
5. Add `ask_user_question_kb` to `keyboards.py`.
6. Extend `formatting.py` with the `AskUserQuestion` branch in
   `_format_assistant_turn`.
7. Create `src/ccr/bot/handlers/ask_user_question.py` with the three
   entry points + the filter. Register the router in `app.py` BEFORE
   `session_router`.
8. Add `ask_user_question_timeout_seconds` to `Settings`.
9. Run `pytest --cov=ccr --cov-fail-under=80`, `ruff check`, `mypy src`.

## Decisions

### Typed-reply correlation: `/answer <id8> <text>` always works; plain text claimed only when exactly one free-text question is outstanding

Two paths but each one is well-defined.

`/answer <id8> <text>` is the authoritative path. Always works. Multi-question safe. Slightly verbose UX but explicit; the prefix is shown in the broadcast so the user does not have to invent it.

Plain text is sugar for the common case (one outstanding free-text question). The filter requires **exactly one** so the routing is unambiguous. With zero outstanding, plain text falls through to the existing `session.handle_text` (becomes a user-turn). With ≥2 outstanding, plain text also falls through; the user sees their message echoed as a normal user-turn and the unanswered questions remain. This is a slight UX trap but an explicit one — and the user always has `/answer` to disambiguate.

Rejected alternatives:
- **Reply-to (Telegram message threading):** clean UX but aiogram's `Message.reply_to_message` field is not always populated (e.g. on web Telegram for channel posts), and broadcast messages are sent from the *bot* — replying to them is well-defined but requires the user to know to long-press. Not worth the dependency on per-client UI.
- **One-at-a-time queue (buffer concurrent questions):** re-introduces the broadcast pause/buffer that CCR-025 deliberately deleted. Each AskUserQuestion already gets its own message + own id; we use the id as the disambiguator instead of timing.
- **Last-open wins:** fragile (user A answers question 1 thinking they're answering question 2 because question 2's keyboard is more recent). No.

### `send_tool_result` lives on `SessionManager`, not `ClaudeProcess`

`ClaudeProcess` already accepts a `list[ContentBlock]` via
`send_user_turn` — that includes `ToolResultBlock`. Adding a parallel
`send_tool_result` on `ClaudeProcess` would duplicate the
`_write_line` consumer with no new wire-level concern (the JSONL
shape is the same).

The interesting bookkeeping is session-level: outstanding-id tracking,
the cancel-on-teardown invariant, the timeout task cancellation. All
of those couple to `SessionManager`, not `ClaudeProcess`. So the new
`send_tool_result` is a manager method that uses `proc.send_user_turn`
internally.

This also keeps `ClaudeProcess` testable in isolation — its tests do
not need to know about AskUserQuestion.

### Concurrent AskUserQuestion is fine; no global pause

Each call gets its own `tool_use_id` → its own keyboard / its own
`/answer <id8>` → its own resolution. Identical to CCR-025's
multi-permission decision.

The free-text plain-text path is the only place where concurrency
matters, and the filter guards it: exactly one outstanding ⇒ plain
text routes there; otherwise plain text falls through. Two paths,
both unambiguous.

### Timeout: 600 s default, configurable via `Settings`

Longer than CCR-025's 120 s because a typed reply often needs more
thought than a permission tap (e.g. "what should I name this
function?" vs. "approve this Bash invocation?"). Configurable via
`ask_user_question_timeout_seconds` (1–3600 same range as
`mcp_permission_timeout_seconds`).

On timeout, `send_tool_result(..., is_error=True)` with content
`f"Timed out — no paired user responded within {N}s"`. Mirrors
CCR-025's deny-on-timeout pattern. The Telegram message is NOT
proactively edited (deferred follow-up — same as CCR-025).

### Stale-reply guard via the same single-shot dict pop

`_pending_questions.pop(tool_use_id, None)` is the single-shot
resolution; either it returns the entry (this caller wins, deliver
the `tool_result`) or `None` (already resolved or unknown — return
`False`). Same shape as `McpPermissionServer.resolve` and
`asyncio.Future.set_result`-with-`InvalidStateError`-suppression.

The timeout task uses `_pending_questions.pop(tool_use_id, None)`
inside its body; it does NOT call `send_tool_result` (which would
also pop) — instead, the timeout task pops and writes the error
`tool_result` directly via `_proc.send_user_turn`. This avoids a
race where the timeout fires after a successful pop but before the
`send_tool_result` write completes.

Implementation detail (sketch): the manager has a private
`_deliver_tool_result(tool_use_id, content, *, is_error)` that
writes the JSONL turn but does NOT touch `_pending_questions`. Both
`send_tool_result` and the timeout task pop the entry first, then
call `_deliver_tool_result`.

### `kind` discriminator on `_PendingKeyboard`

Two consumers (perm, auq); a tag dispatch in
`_materialise_keyboards` is one extra `if`. Cheaper than a sibling
sentinel class with parallel handling code in two places. Default
`kind="perm"` keeps the CCR-025 callsite untouched.

### `_pending_questions` lives on `SessionManager`, not on a separate class

Per CCR-025's "no abstraction over the Future-keying scheme"
precedent. Two dicts (well, one dict + dataclass values) and a
timeout task per question — all session-scoped, all cleared on
teardown. Hoisting to a class would shadow `McpPermissionServer`'s
shape without the lifetime distinction (MCP's server outlives
sessions; pending questions do not). Keep it inline.

### Why `send_tool_result` returns `bool`, not raises

Same rationale as `resolve_permission` (CCR-025 plan §"Why
resolve_permission returns bool"): the bot needs to distinguish
"successfully resolved" from "stale / unknown" so it can pick the
right alert. Returning `bool` is the simplest interface — no new
exception types, no enum to extend. The `NoActiveSessionError`
raise is reserved for the genuinely-impossible case (`_proc is
None`), which is a developer error, not a stale-prompt user-error.

### Step-0 probe is on the developer, not the architect

CCR-026 inherits the CCR-021/CCR-020 step-0 precedent. The
architect specifies the **best-effort** wire shape based on public
Anthropic docs and the existing repo evidence (`AskUserQuestion` is
listed in Claude's `tools` array, suggesting a normal `tool_use`
block). The developer runs a real `claude -p` session to confirm
the `tool_input` schema before writing code; if outcome (3) (the
shape is fundamentally different — e.g. routed via MCP, or carries
a different content shape), the developer returns `BLOCKED` rather
than improvising. This mirrors the CCR-021 escape hatch.

## Open questions for team lead

None that block the developer. One soft caveat:

- **`tool_input` schema is unprobed.** The plan assumes
  `{"question": str, "options": list[str]}`. Real Claude may use
  `prompt` instead of `question`, or carry options as objects with
  `value` / `label` keys. The Step-0 probe (a 5-minute real-claude
  invocation) settles this before code lands. This is a developer
  step, not a team-lead step. If the probe hits outcome (3) — no
  `tool_use` block at all — the developer escalates with `BLOCKED`
  per CCR-021's precedent and we re-design.

If team lead would prefer the architect to run the probe before
dispatching the developer (rather than letting the developer do it
as Step 0), say so — I can add a "probe transcript" requirement to
the architect's plan-file output. Per the brief's "architect must
NOT prescribe a real-binary probe" rule (CCR-025 plan §Open
questions), I have not run the probe; the developer does it.
