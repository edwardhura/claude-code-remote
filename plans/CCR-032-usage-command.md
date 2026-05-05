# Plan: CCR-032 — `/usage` command (account-wide quota / rate-limit summary)

## Goal

Surface claude's account-wide rate-limit / quota state as a rich, locally-rendered Telegram reply when a paired user sends `/usage`. Today the catch-all passthrough router would either forward `/usage` to claude (producing a useless one-liner — see Step 0 probe) or fall through to `_UNKNOWN_USAGE_HINT`. After this ticket, `/usage` is handled by a dedicated branch in `passthrough.py` that reads the most recent `rate_limit_event` snapshot held in memory by `SessionManager` and renders it as HTML, with a canonical `"No active session."` reply when idle.

The shape mirrors CCR-023's `/cost` lift-out: a dedicated branch, a thin accessor on `SessionManager`, and a render helper next to it. No new module is added — `/cost`'s aggregator (`usage.py`) is **not** extended.

## Step 0 probe — outcome and rationale

Probe transcript: `/Users/edward/workspace/claude-code-remote/tmp/ccr-032-probe-1777844357.jsonl` (10 lines, 32 KB; stderr is empty). Probe driver: `tmp/probe_ccr_032.py` (kept for reference; not committed).

The probe sent three user turns (`"Hello"`, `"What is 2+2?"`, `"/usage"`) over `claude -p --input-format=stream-json --output-format=stream-json --verbose` (claude 2.1.126) in `/private/tmp/ccr-032-probe-cwd`. Findings, in order of importance:

1. **`/usage` IS a recognised slash command on claude's side.** Every `system/init` event lists it in `slash_commands` (alongside `extra-usage`, `clear`, `compact`, `context`, `init`, `usage`, etc.). The `-p` runtime accepted the `/usage` user turn without error.

2. **But claude's `-p` reply to `/usage` is a useless one-liner with no structured data.** The assistant turn produced exactly:

   > `You are currently using your subscription to power your Claude Code usage`

   The accompanying `result/success` event is a tell: `num_turns=0`, `usage.input_tokens=0`, `usage.output_tokens=0`, `duration_ms=2`. Claude short-circuited the slash command without going to the model — it returned canned subscription text. There is **no** structured detail to post-process. Forwarding `/usage` via `send_slash` would just relay this canned line to chat, which is strictly worse than what we can render locally. This rules out the simple-forward path of Outcome 2.

3. **The real substrate is `rate_limit_event`, emitted out-of-band by claude itself.** One line was emitted in this probe, right after the first assistant turn (line 3 of 10). Its full shape:

   ```json
   {
     "type": "rate_limit_event",
     "rate_limit_info": {
       "status": "allowed",
       "resetsAt": 1777861800,
       "rateLimitType": "five_hour",
       "overageStatus": "rejected",
       "overageDisabledReason": "group_zero_credit_limit",
       "isUsingOverage": false
     },
     "uuid": "758cb741-0e54-4af1-9c32-0b76648f795f",
     "session_id": "831d0859-e616-4ad0-9ff4-047eb0ae1b81"
   }
   ```

   This matches what the CCR-023 probe transcript (`tmp/ccr-023-probe-1777812587.jsonl`) showed — same shape, same fields. The event was emitted **once near the start of the session** and not re-emitted on subsequent user turns (including the `/usage` turn). Semantics: this is a **per-line snapshot**, last-one-wins. There is no aggregation across lines — the most recent `rate_limit_event` *is* the current rate-limit state.

**Conclusion: this is Outcome 1 (rate-limit-event-driven), with Outcome 2's simple-forward path explicitly rejected because claude's own `/usage` reply is degenerate.** The implementation pulls from the in-memory snapshot held by `SessionManager`, ignoring claude's own `/usage` text.

### Why **not** extend `usage.py`'s JSONL walker

The ticket Notes name this temptation explicitly. `aggregate_session_usage` is a **per-`result` accumulator** — it folds every `result` event's `usage`, `duration_ms`, `total_cost_usd` into a running sum. Rate-limit state is a **per-line snapshot** with last-one-wins semantics. The two are different aggregation patterns:

- Composing them in one walker would mean either (a) walking the JSONL twice with two semantics in `SessionUsage` (a leaky composite), or (b) returning a tuple / two dataclasses from one walker (couples unrelated lifetimes — a `/cost` caller now pays the rate-limit walk cost and vice versa, and a future `/cost` change risks breaking `/usage`).
- The data is already in memory in the consumer task — the JSONL walk is needless work for a piece of state we have hot.
- The `_skills` snapshot (CCR-030) is the exact precedent: a piece of session state surfaced by an event, held as an instance attribute updated by `_consume_events`, and exposed via a read-only accessor. `/usage` is structurally identical.

So `usage.py` stays untouched.

### Why a first-class `RateLimitEvent` Pydantic model rather than `UnknownEvent.raw` inspection

The probe shows a stable, named shape (`type: "rate_limit_event"` with a `rate_limit_info` sub-object). Modelling it gives:

- `model_config = ConfigDict(extra="allow")` so unknown future fields surface via `model_extra` rather than failing — same drift-tolerance as every other event in `events.py`.
- Static typing on the consumer in `manager.py` (matters for `mypy --strict`).
- A clear pivot point if the field set drifts: change the model, not the consumer.

The known fields (`status`, `resetsAt`, `rateLimitType`, `overageStatus`, `overageDisabledReason`, `isUsingOverage`) are stable enough across both the CCR-023 and CCR-032 probes to model directly. Optional / nullable everywhere (per `extra="allow"` philosophy) so a missing field does not flip the snapshot to invalid.

## Files

- `src/ccr/claude/events.py` — **modify**. Add `RateLimitInfo` and `RateLimitEvent` Pydantic models. Add `RateLimitEvent` to the `_KnownEvent` discriminated union (so `parse_event` returns it directly rather than `UnknownEvent`). Add to `ClaudeEvent` union and `__all__`.
- `src/ccr/claude/manager.py` — **modify**. Add a `_rate_limit_status: RateLimitEvent | None` instance attribute (reset on `new_session` / `continue_session` / `_teardown_locked`, populated in `_consume_events` on every `RateLimitEvent`). Add `current_rate_limit_status() -> RateLimitEvent | None` accessor next to `current_session_usage()`. Import `RateLimitEvent`.
- `src/ccr/bot/handlers/passthrough.py` — **modify**. Lift `/usage` out of the unknown-fallthrough into a dedicated `_reply_usage` branch (mirroring `/cost`). Add `_render_usage_reply(rate_limit: RateLimitEvent) -> str`. Update `_UNKNOWN_USAGE_HINT` to include `/usage` (mirroring CCR-030's `/skills` precedent).
- `tests/test_bot_passthrough.py` — **modify**. Extend `FakeManager` with a `rate_limit` attribute and `current_rate_limit_status()` method. Add `/usage` test cases (see Test surface below). Keep CCR-023 `/cost` regressions, CCR-010 `/model` + `/compact` regressions.
- `tests/test_session_manager.py` — **modify**. Add tests for `current_rate_limit_status()` lifecycle: idle returns `None`; live session populates after `RateLimitEvent` arrives via fake_claude; returns `None` after `stop()`.
- `tests/test_claude_events.py` (existing — confirmed in the repo) — **modify**. Add a parse test for `rate_limit_event` confirming it now surfaces as `RateLimitEvent` rather than `UnknownEvent`, and that unknown extra fields fall through `model_extra`.

No new file is added. `usage.py` is **not** modified — explicitly out of scope per the design rationale above.

## Public surface

### `src/ccr/claude/events.py`

```python
# Sketch — illustrative, not the final code.

class RateLimitInfo(BaseModel):
    """Inner ``rate_limit_info`` payload of :class:`RateLimitEvent`.

    Forward-compatible: every field is optional and ``extra="allow"`` so
    new server-side fields surface in ``model_extra`` rather than failing.
    """
    model_config = ConfigDict(extra="allow")
    status: str | None = None              # e.g. "allowed", "warning"
    resets_at: int | None = Field(default=None, alias="resetsAt")  # epoch seconds
    rate_limit_type: str | None = Field(default=None, alias="rateLimitType")  # e.g. "five_hour"
    overage_status: str | None = Field(default=None, alias="overageStatus")
    overage_disabled_reason: str | None = Field(default=None, alias="overageDisabledReason")
    is_using_overage: bool | None = Field(default=None, alias="isUsingOverage")
    # NB: ConfigDict must also enable populate_by_name=True so the parser can
    # accept either camelCase (claude wire) or snake_case (test fixtures).


class RateLimitEvent(_EventBase):
    """Out-of-band rate-limit / quota snapshot from Claude Code."""
    type: Literal["rate_limit_event"]
    rate_limit_info: RateLimitInfo | None = None
    session_id: str | None = None
    uuid: str | None = None


_KnownEvent = Annotated[
    SystemInit | UserTurn | AssistantTurn | ResultEvent | RateLimitEvent,
    Field(discriminator="type"),
]

ClaudeEvent = (
    SystemInit | UserTurn | AssistantTurn | ResultEvent | RateLimitEvent | UnknownEvent
)
```

Add `RateLimitEvent` and `RateLimitInfo` to `__all__`.

### `src/ccr/claude/manager.py`

```python
# Sketch — illustrative, not the final code.

# In __init__:
self._rate_limit_status: RateLimitEvent | None = None

# In new_session() / continue_session() reset block (mirror _skills = []):
self._rate_limit_status = None

# In _teardown_locked() final reset block (mirror _skills = []):
self._rate_limit_status = None

# In _consume_events(), inside the async-for, alongside the SystemInit /
# ResultEvent branches:
if isinstance(event, RateLimitEvent):
    self._rate_limit_status = event

# Public accessor (alongside current_session_usage()):
def current_rate_limit_status(self) -> RateLimitEvent | None:
    """Return the most recent rate-limit snapshot for the live session.

    Returns ``None`` if no session is running OR no ``rate_limit_event``
    has been observed yet on this session. Last-one-wins: a later
    ``rate_limit_event`` replaces the snapshot.
    """
    if self._proc is None:
        return None
    return self._rate_limit_status
```

`current_rate_limit_status` is **not** async (mirrors `available_skills`, `running_subagents`, `is_session_active`) — it's a pure in-memory read.

### `src/ccr/bot/handlers/passthrough.py`

```python
# Sketch — illustrative, not the final code.

_UNKNOWN_USAGE_HINT = (
    "Unknown command. Whitelisted: /new /stop /clear /view /last /preview "
    "/cost /usage /model /compact /who /agents /skills."
)


def _format_resets_at(epoch_seconds: int | None) -> str:
    """Render ``resets_at`` as a UTC ISO timestamp + relative delta.

    Returns ``"unknown"`` when ``epoch_seconds`` is None or non-positive.
    Delta uses :func:`_format_elapsed`-style ``Xm Ys`` for < 1 h or
    ``Xh Ym`` otherwise; "now"/"in the past" cases return a sane string.
    """
    ...


def _render_usage_reply(rl: RateLimitEvent) -> str:
    """Render a rich HTML ``/usage`` reply.

    Layout — every interpolated value HTML-escaped, every field optional
    (a missing field is omitted from the reply, never rendered as
    ``"None"``):

        <b>Rate-limit window:</b> {rate_limit_type}
        <b>Status:</b> {status}
        <b>Resets at:</b> {iso} (in {delta})
        <b>Overage:</b> {overage_status}{ optional " — {overage_disabled_reason}" }
        <b>Using overage:</b> {yes/no}

    Output is truncated to ``SAFE_CHUNK`` defensively, mirroring
    ``_render_cost_reply``.
    """
    info = rl.rate_limit_info
    if info is None:
        return "Rate-limit info unavailable."
    lines: list[str] = []
    if info.rate_limit_type:
        lines.append(f"<b>Rate-limit window:</b> {html.escape(info.rate_limit_type)}")
    if info.status:
        lines.append(f"<b>Status:</b> {html.escape(info.status)}")
    if info.resets_at:
        lines.append(f"<b>Resets at:</b> {_format_resets_at(info.resets_at)}")
    if info.overage_status:
        suffix = ""
        if info.overage_disabled_reason:
            suffix = f" — {html.escape(info.overage_disabled_reason)}"
        lines.append(f"<b>Overage:</b> {html.escape(info.overage_status)}{suffix}")
    if info.is_using_overage is not None:
        lines.append(f"<b>Using overage:</b> {'yes' if info.is_using_overage else 'no'}")
    if not lines:
        return "Rate-limit info unavailable."
    text = "\n".join(lines)
    if len(text) > SAFE_CHUNK:
        text = text[:SAFE_CHUNK]
    return text


async def _reply_usage(msg: Message, session_manager: SessionManager) -> None:
    """Answer ``/usage`` with the most recent rate-limit snapshot.

    Returns ``"No active session."`` if no Claude subprocess is currently
    held (mirrors ``/cost`` and ``/skills``). Returns
    ``"No rate-limit data received yet on this session."`` when a session
    is active but no ``rate_limit_event`` has been observed yet — this is
    expected for the first few seconds after ``/new`` until claude emits
    its first snapshot.
    """
    if not session_manager.is_session_active():
        await msg.answer("No active session.")
        return
    rl = session_manager.current_rate_limit_status()
    if rl is None:
        await msg.answer("No rate-limit data received yet on this session.")
        return
    await msg.answer(_render_usage_reply(rl))


# In cmd_passthrough(), alongside the /agents, /skills, /cost branches:
if name == "usage":
    await _reply_usage(msg, session_manager)
    return
```

The `"No rate-limit data received yet on this session."` second-tier reply is deliberate — using `"No active session."` for both states would lie to the user about the session's existence. The two messages are clearly distinguishable in tests and at the chat surface.

## Patterns and prior art

- **Reuse: `_skills` snapshot pattern** — `src/ccr/claude/manager.py:143` (instance attribute), `:229` (reset on `new_session`), `:357` (reset on `continue_session`), `:537` (reset on `_teardown_locked`), `:561-563` (populate in `_consume_events`), `:842` (read-only accessor). `_rate_limit_status` mirrors this exactly. The only structural difference: `_skills` is a `list[str]` reset to `[]`; `_rate_limit_status` is `RateLimitEvent | None` reset to `None` (because "no event seen yet" needs to be distinguishable from "event seen, info empty").
- **Reuse: forward-compatible event model** — `src/ccr/claude/events.py:138` (`ResultEvent` shape) and `:148` (`UnknownEvent`). `RateLimitEvent` follows the same `ConfigDict(extra="allow")` philosophy. The `populate_by_name=True` setting on `RateLimitInfo` is needed because claude wire fields are camelCase but Python convention is snake_case — the Pydantic alias system handles the mapping.
- **Reuse: `/cost` branch shape** — `src/ccr/bot/handlers/passthrough.py:142` (`_render_cost_reply`), `:183` (`_reply_cost`), `:217` (the `if name == "cost":` branch in `cmd_passthrough`). `/usage` follows this layout 1:1: a render helper, an async branch handler, a single-line dispatch in `cmd_passthrough`.
- **Reuse: `_UNKNOWN_USAGE_HINT` update** — `src/ccr/bot/handlers/passthrough.py:60-63`. Adding `/usage` to the hint mirrors the CCR-030 `/skills` precedent so grepping for `/usage` in the source returns the hint as well as the handler.
- **Reuse: `SAFE_CHUNK` truncation** — defensive cap, mirroring `_render_cost_reply` (`passthrough.py:178-179`).
- **Reuse: `html.escape` everywhere** — every interpolated string field comes from claude's wire format and is treated as untrusted, matching CCR-008 / CCR-018 / CCR-022 / CCR-023 conventions.
- **Avoid: extending `usage.py`'s `aggregate_session_usage` walker** — wrong aggregation pattern (per-line snapshot vs per-`result` accumulator), wrong locality (data is hot in memory), wrong coupling (one walker for two unrelated lifetimes). See the rationale block above.
- **Avoid: forwarding `/usage` via `send_slash`** — claude's own reply is a useless canned one-liner (probe line 9). Forwarding would be strictly worse than the local render. This is the same reasoning CCR-023 used to lift `/cost` out of `WHITELIST`.
- **Avoid: combining `_rate_limit_status` with `current_session_usage()`** — they are different lifetimes (snapshot vs aggregate), different sources (live event vs JSONL walk), different consumers (`/usage` vs `/cost`). Keeping them separate is the same separation `_skills` keeps from `running_subagents`.

## Abstractions

**No new abstraction.** Three lift-out branches in `passthrough.py` (`/agents`, `/skills`, `/cost`) and now a fourth (`/usage`) — the rule of three is met but each branch's *render shape* is distinct (subagent list vs skill list vs token aggregate vs rate-limit snapshot), so a generic "local-render slash command" abstraction would have to thread four different render contracts through one signature. Concrete repetition wins. The `_reply_X` / `_render_X_reply` pair stays.

`RateLimitEvent` and `RateLimitInfo` are *new types*, not new abstractions — they model an existing wire-format event that the codebase has been silently dropping into `UnknownEvent` since CCR-007. This is the same kind of additive modelling CCR-030 did for the `skills` field on `SystemInit`.

## Dependencies

- **Depends on**:
  - `ccr.claude.events` — extends the `_KnownEvent` union and `ClaudeEvent` type alias.
  - `ccr.claude.manager.SessionManager` — extends the in-memory state and accessor surface.
  - `ccr.bot.handlers.passthrough.cmd_passthrough` — adds a branch.
  - `ccr.bot.formatting.SAFE_CHUNK` — already imported.
  - `html` stdlib — already imported in `passthrough.py`.
- **Used by**:
  - The bot's `/usage` slash command (this ticket's only direct consumer).
  - Future SSE / web-viewer renderings of rate-limit state could read `RateLimitEvent` from the JSONL stream once it's a known type — but that's a follow-up ticket. This ticket only ships the bot path.

## Edge cases the developer must handle

1. **`rate_limit_info` is `None` or empty.** The probe always carries it, but `extra="allow"` means a future schema drop could land it nullable. The render helper must check `info is None` (returns `"Rate-limit info unavailable."`) and must skip individual `None` fields rather than rendering `"None"`.
2. **No session active.** `_reply_usage` calls `session_manager.is_session_active()` first and returns the canonical `"No active session."` string. This is the same precedent as `/skills` (`passthrough.py:120`) and `/cost` (`passthrough.py:192-194`).
3. **Session active but no `rate_limit_event` seen yet.** Real probes show the event lands shortly after the first assistant turn — a user typing `/usage` in the first 2 s of `/new` may hit the empty-state. The reply is `"No rate-limit data received yet on this session."` — distinct from the no-active-session string so tests and users can tell them apart. This must be tested.
4. **`is_using_overage: False` vs `None`.** `if info.is_using_overage:` would conflate the two (both falsy). Use `is not None` checks.
5. **`resets_at` epoch in the past.** Possible if claude emits a stale snapshot (or in test fixtures with hard-coded values). The formatter should render the absolute UTC time always; the relative delta should say something like `"in the past"` rather than producing a negative duration.
6. **Field aliases (camelCase wire ↔ snake_case Python).** `RateLimitInfo` needs `populate_by_name=True` AND each aliased field must declare `Field(default=None, alias="resetsAt")` etc. Without this, parsing the live wire format will silently drop those fields into `model_extra`.
7. **HTML escape every string.** `status`, `rate_limit_type`, `overage_status`, `overage_disabled_reason` all come from claude's wire format and must be `html.escape()`-d. The probe shows benign values today but the bot speaks HTML mode.
8. **`SAFE_CHUNK` truncation.** Mirror `_render_cost_reply`'s defensive cap. The known field set is short, but a future rogue field could push the rendered text over Telegram's 4096-byte cap; truncation is cheap insurance.
9. **`_rate_limit_status` reset symmetry.** Three reset sites in `manager.py` (`new_session`, `continue_session`, `_teardown_locked`). Missing any one would leak rate-limit state across sessions, which would be a correctness bug (a user paired to a different account could see the wrong account's state). Tests must cover the teardown reset.
10. **Existing `UnknownEvent` parse path.** Before this ticket, `rate_limit_event` parsed as `UnknownEvent(type="rate_limit_event", raw={...})`. Adding it to `_KnownEvent` flips the parse output to `RateLimitEvent`. Confirm no callsite was branching on `UnknownEvent.type == "rate_limit_event"` (a quick `grep` should be empty — the codebase had no awareness of this event type before today).
11. **`_consume_events` ordering.** The new `isinstance(event, RateLimitEvent)` branch should sit alongside the `SystemInit` and `ResultEvent` branches, **after** the `log_obj.append` + `bus.publish` but **before** `_track_subagents` + `_schedule_last_event_at_update` — the snapshot is in-memory bookkeeping, identical timing to `_skills`.
12. **Bus + JSONL still see `RateLimitEvent`.** `_consume_events` calls `log_obj.append(event)` and `bus.publish(...)` for every event regardless. Bot broadcast and SSE replay see `RateLimitEvent` for free. Bot broadcast must NOT render rate-limit events as a chat message — `formatting.py::event_to_messages` does not currently have a branch for them, and the `match`/`isinstance` dispatch falls through to ignore unknown variants. Verify (read `formatting.py`) that the new known type does not accidentally trigger an existing branch. **If it does, add a no-op branch returning `[]`** so `RateLimitEvent` is silently dropped from the chat broadcast (it's a snapshot, not a chat-worthy event).

## Test surface

All test additions are sketches — bullet items, not implementation. Tests are written by the developer.

### `tests/test_claude_events.py`

- `test_parse_rate_limit_event_returns_typed_model` — given a probe-shape JSON line, `parse_event` returns a `RateLimitEvent` (not `UnknownEvent`); `info.rate_limit_type == "five_hour"`; `info.is_using_overage is False`; `info.resets_at == 1777861800`.
- `test_parse_rate_limit_event_alias_round_trip` — `RateLimitInfo` accepts both camelCase wire (`resetsAt`) and snake_case (`resets_at`) inputs (covers test fixtures that use snake_case).
- `test_parse_rate_limit_event_extra_fields_fall_through_model_extra` — an unknown sub-field (e.g. `"newField": "foo"`) ends up in `model_extra` rather than failing validation.
- `test_parse_rate_limit_event_missing_info_object` — a `rate_limit_event` line without `rate_limit_info` parses cleanly with `info is None` (forward-compat).

### `tests/test_session_manager.py`

- `test_current_rate_limit_status_idle_returns_none` — fresh `SessionManager`, no session — accessor returns `None`.
- `test_current_rate_limit_status_populates_after_event` — fixture JSONL with `system/init` + `rate_limit_event` + `result/success`; start session via fake_claude; poll until consumer drains (mirror existing `test_running_subagents_*` cadence); assert `current_rate_limit_status()` returns a `RateLimitEvent` whose `rate_limit_info.rate_limit_type == "five_hour"`.
- `test_current_rate_limit_status_last_event_wins` — fixture JSONL with two `rate_limit_event` lines, second carrying a different `rateLimitType` (e.g. `"weekly"`); assert the accessor returns the second snapshot.
- `test_current_rate_limit_status_resets_on_stop` — start a session that observes a `rate_limit_event`, call `stop()`, assert accessor returns `None`.

### `tests/test_bot_passthrough.py`

- `test_usage_active_session_with_rate_limit_renders_html` — `FakeManager(active=True, rate_limit=<probe-shape>)`; assert `msg.answer.call_args[0][0]` contains `"Rate-limit window:"`, `"five_hour"`, `"allowed"`; assert `send_slash` was NOT called (this is the explicit non-call assertion the ticket calls out).
- `test_usage_no_active_session_returns_canned_string` — `FakeManager(active=False)`; assert reply is exactly `"No active session."`.
- `test_usage_active_session_no_rate_limit_yet_returns_distinct_string` — `FakeManager(active=True, rate_limit=None)`; assert reply is exactly `"No rate-limit data received yet on this session."` (distinct from the no-active-session canned string).
- `test_usage_html_escapes_interpolated_values` — pass a `rate_limit_type` containing `<script>` and an `overage_disabled_reason` containing `&` / `<`; assert the reply contains `&lt;script&gt;` and `&amp;` and does NOT contain raw `<script>`.
- `test_usage_omits_missing_optional_fields` — `RateLimitInfo` with only `status="allowed"` set, all other fields `None`; assert the reply renders only the status line and does not contain `"None"` anywhere.
- `test_usage_renders_using_overage_false_distinct_from_none` — confirms `is_using_overage: False` renders `"no"` while `is_using_overage: None` omits the line entirely.
- `test_usage_resets_at_in_the_past` — pass a `resets_at` from 2020; assert the reply contains an absolute UTC timestamp and a "in the past" (or sane equivalent) marker rather than a negative duration.
- `test_unknown_usage_hint_includes_usage_command` — assert `_UNKNOWN_USAGE_HINT` mentions `/usage` (regression for the CCR-030 grep-stability precedent).
- **Regression cases** (mirror the seven CCR-023 `/cost` tests):
  - `test_cost_active_session_renders_html` — unchanged from CCR-023.
  - `test_cost_no_active_session_returns_canned_string` — unchanged from CCR-023.
  - `test_model_whitelist_forwards_via_send_slash` — unchanged from CCR-010.
  - `test_compact_whitelist_forwards_via_send_slash` — unchanged from CCR-010.
  - `test_unknown_command_returns_usage_hint` — adjusted for the updated hint string.

### Test fixtures

- The probe-shape `rate_limit_event` JSON line goes into a JSONL fixture at the test location used by `test_session_manager.py`'s existing fake_claude harness. **No change to `tests/fakes/fake_claude.py`** — it already handles arbitrary JSONL via `FAKE_CLAUDE_SCRIPT`. (The ticket Files block lists `fake_claude.py` as "extend with synthetic rate_limit_event lines" — interpret that as **extending the fixture JSONL passed to fake_claude, not the script itself**.)

## Out of scope

- **Real-money pricing / Anthropic billing API calls** — explicit ticket exclusion.
- **Persisting quota / rate-limit history across sessions in SQL** — ticket explicit; the SQL schema does not change.
- **Account-management UX (changing plans, viewing invoices)** — ticket explicit.
- **Modifying `/cost`, `/model`, `/compact`, `/agents`, `/skills`, `/clear`** — ticket explicit; only regression coverage.
- **Extending `usage.py`** — explicitly rejected by the design rationale (wrong aggregation pattern).
- **Forwarding `/usage` to claude (Outcome 2 path)** — explicitly rejected by the probe (claude's reply is a useless canned one-liner).
- **A `formatting.py::event_to_messages` branch for `RateLimitEvent`** — out of scope for this ticket. Today the dispatch falls through to ignore unknown variants; verify this still holds after the type is moved into `_KnownEvent`. Surfacing rate-limit events in chat broadcast is a separate UX call.
- **Web-viewer / SSE rendering of `RateLimitEvent`** — the type is now first-class for any consumer that wants it, but only the bot's `/usage` is wired up in this ticket.
- **An `extra-usage` slash command** — claude's `system/init` lists it; the bot does not surface it. Out of scope.

## Open questions for team-lead

None. The design is determined: probe outcome is unambiguous, prior art is direct, no smuggled scope. Developer can proceed.

## Risks

- **Schema drift on `rate_limit_info`.** Anthropic could rename `resetsAt` → `resets_at` or split `rate_limit_type` into multiple fields. Mitigation: every field is optional, `extra="allow"` everywhere, and every render-helper line guards on the field's presence. A drop-in field renames simply renders fewer lines until the alias is updated. Tests cover the "missing field" path.
- **`rate_limit_event` arrives later than the user's first `/usage` poll.** Empty-state reply is distinct (`"No rate-limit data received yet on this session."`) so the user is not lied to. Probe shows the event lands within ~1 s of the first assistant turn, so the empty window is small in practice.
- **The probe captured only `five_hour` rate-limit type and `allowed` status.** Other values (`weekly`, `warning`, `denied`, etc.) are not exercised by the probe transcript. Mitigation: tests pass arbitrary strings into the renderer (per `html.escape` and string-passthrough semantics); the model field is `str | None`. A real `denied` state would render as `Status: denied` — informative even without a UX special-case. A follow-up ticket can add severity-based formatting if the user asks for it.
- **`formatting.py` adding a chat-broadcast branch later.** If a future ticket wants to surface `RateLimitEvent` in chat, the type is already first-class. No design call here, just flagging that the door is open.
