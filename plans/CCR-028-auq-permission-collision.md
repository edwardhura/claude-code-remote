# Plan: CCR-028 — AskUserQuestion / MCP permission gate collision

## Goal

Real-Claude routes the built-in `AskUserQuestion` tool through the
`--permission-prompt-tool` MCP channel — contradicting CCR-026's
"orthogonal to `McpPermissionRequest`" assumption. The user-visible
failure today: when claude calls `AskUserQuestion`, the bot broadcasts
BOTH the `❓` AUQ keyboard AND a separate `🛑 Permission requested` MCP
prompt for the same `tool_use_id`; whichever resolves first wins, the
loser desyncs. On the `allow` path the harness then writes its own
empty native fulfilment (`"User has answered your questions: ."`),
which races our `send_user_turn(ToolResultBlock(...))` injection and
makes claude report "tool response came back empty".

This ticket fixes the collision so:

1. `AskUserQuestion` produces exactly ONE chat surface — the AUQ
   keyboard — never a parallel `🛑 Permission requested` message.
2. Tapping an AUQ option resolves the MCP Future (the harness writes
   ONE synthetic `tool_result`); it does NOT also write a second
   `tool_result` via `send_user_turn`.
3. The pattern is parameterised on a `frozenset[str]` of suppressed
   tool names so CCR-027 (`ExitPlanMode`) can reuse it without
   re-architecting.

After this ticket, a real `claude -p` session that triggers
`AskUserQuestion` round-trips through Telegram exactly once, claude
receives the chosen answer, and proceeds.

## Step-0 probe outcome

Probe ran in this environment with claude `2.1.128` and the project's
real `McpPermissionServer`. Three transcripts captured under `tmp/`:

- `tmp/ccr-028-probe-1777969761.{jsonl,tool-calls.jsonl,summary.txt}`
  — decision `allow`. Outcome: `_on_tool_call` fires once with
  `tool_name == "AskUserQuestion"` (verbatim, NOT scoped to
  `mcp__ccr__AskUserQuestion`); `tool_input` is the same nested
  `questions[]` schema CCR-026 already probed. After we returned
  `{"behavior": "allow", "updatedInput": <input>}`, claude proceeded
  with its own native fulfilment — a `tool_result` whose content is
  `"User has answered your questions: . You can now continue with the
  user's answers in mind."` and `tool_use_result.answers == {}`. **This
  is the empty-fulfilment race the ticket describes.** No second MCP
  call.
- `tmp/ccr-028-probe-1777969866.*` — decision `deny`, message
  `"probe deny"`. Outcome: `_on_tool_call` fires once. After we
  returned `{"behavior": "deny", "message": "probe deny"}`, claude
  emitted ONE `user`-turn `tool_result` with
  `is_error=True, content='probe deny', tool_use_id=<the AUQ id>`.
  **No native fulfilment.** Claude then proceeded with `"Got it."` and
  recorded the call in `result.permission_denials`. The harness treats
  the deny payload as the tool's terminal output for that
  `tool_use_id`.
- `tmp/ccr-028-probe-1777969959.*` — decision `block` (sleep past the
  timeout). Outcome: `_on_tool_call` fires once; the AssistantTurn
  carrying the AUQ `tool_use` block is emitted on stdout BEFORE the
  MCP call lands; claude then halts entirely until MCP resolves. Probe
  was killed at 100 s. Confirms `_on_tool_call` blocks the entire
  AssistantTurn → tool_result cycle for AUQ — there is no native
  fulfilment in flight while we own the Future.
- Bonus probe `/tmp/probe_ccr028_real_answer.py` confirmed that
  `{"behavior": "deny", "message": "Red"}` is treated by claude as the
  user's answer: claude generated an assistant text describing "red"
  ("you might like it because red signals energy and confidence ...").
  The `is_error=True` flag does NOT confuse downstream reasoning.

Five load-bearing facts the design follows:

1. `_on_tool_call` IS in the path for `AskUserQuestion`. The CCR-026
   plan was wrong on that point; the bug is real.
2. `_on_tool_call` receives `tool_name + tool_input` only — **never the
   `tool_use_id`**. Correlation between the MCP call and the
   `_pending_questions[tool_use_id]` entry must come from the
   AssistantTurn JSONL stream, not from MCP.
3. The AssistantTurn JSONL line precedes the MCP call (claude emits
   the `tool_use` then invokes the permission prompt). So
   `_track_ask_user_question` reliably runs first; the suppressed
   handler can pair the incoming MCP request to the FIFO-first
   `_pending_questions` entry without an MCP id.
4. AUQ calls are serialised within a session: claude waits for the
   tool_result before the next assistant turn, so at most one AUQ MCP
   request is outstanding at a time. FIFO pairing is unambiguous.
5. `tool_name` arrives unscoped (`"AskUserQuestion"`, not
   `"mcp__ccr__AskUserQuestion"`). Suppression keys against the bare
   name.

## File layout

- `src/ccr/claude/mcp.py` — **modify** — `McpPermissionServer.__init__`
  gains `suppressed_tool_names: frozenset[str] = frozenset()`. New
  callback registration `set_suppressed_tool_handler(cb)` that
  `SessionManager` uses to claim suppressed-tool MCP calls. Internal
  `_on_tool_call` branches on `tool_name in self._suppressed_tool_names`:
  for suppressed names, mints `request_id` and registers the Future
  exactly as today, **but skips the bus publish** and instead invokes
  the registered handler with `(request_id, tool_name, tool_input)`.
  Resolution flows through the existing `resolve(request_id, decision)`
  / `_resolve_internal` path — no second resolution channel.
- `src/ccr/claude/manager.py` — **modify** — `SessionManager.__init__`
  passes `suppressed_tool_names=frozenset({"AskUserQuestion"})` to
  `McpPermissionServer` and registers a handler
  `_on_mcp_ask_user_question(request_id, tool_name, tool_input)` via
  `set_suppressed_tool_handler`. The handler pairs the MCP request
  with `_pending_questions` (FIFO; falls back to a small
  `_unpaired_mcp_auq_calls: collections.deque[str]` when the MCP call
  arrives before the AssistantTurn). `_track_ask_user_question` is
  extended to consume from that deque on registration. `_PendingQuestion`
  gains `mcp_request_id: str | None` (default `None`). `send_tool_result`
  changes shape: when the popped entry has `mcp_request_id is not None`
  (i.e. AUQ via MCP), resolve the MCP Future with
  `{"behavior": "deny", "message": <answer>}` and DO NOT call
  `_deliver_tool_result`. When `mcp_request_id is None` (legacy /
  defensive path; also covers `is_error=True` timeout writes that we
  must keep working), keep the existing wire path. `_handle_question_timeout`
  is updated symmetrically: on timeout for an MCP-paired AUQ, resolve
  the Future with deny + canned timeout message; on timeout for an
  unpaired entry (shouldn't happen in production but defensive) keep
  the old `_deliver_tool_result` path. `_teardown_locked` continues to
  call `_mcp.cancel_pending(session_id)` which already drains every
  outstanding Future with deny — that includes AUQ Futures — so AUQ
  teardown is the same code path.
- `src/ccr/bot/formatting.py` — **modify** — defensive guard:
  `_format_mcp_permission` returns `[]` (or callers skip it) when
  `event.tool_name == "AskUserQuestion"` (and any future suppressed
  name). The primary suppression is at MCP source — the bus envelope
  is never published — but the guard protects against schema drift
  and lets the test surface assert the suppression behaviour at the
  formatter level too. Implemented as a small module-level constant
  `_AUQ_TOOL_NAMES = frozenset({"AskUserQuestion"})` that mirrors the
  manager-level constant; comment cross-references both sites.
- `src/ccr/server.py` — **no signature change** — broadcast loop
  already handles `event_to_messages` returning `[]` (an empty list
  short-circuits at line 120: `if not messages: continue`). No
  modification needed.
- `src/ccr/claude/__init__.py` — **no change** — `McpPermissionServer`
  re-export already exists.
- `tests/test_mcp_tool.py` — **modify** — add cases for the new
  suppressed-tool path: (a) `AskUserQuestion` tool call invokes the
  registered handler instead of publishing to the bus; (b) resolving
  via `resolve(request_id, ...)` still unblocks the in-flight call;
  (c) timeout still writes deny; (d) when no handler is registered,
  the suppressed call falls through to the legacy bus-publish path
  (back-compat / defence in depth). Existing tests for the non-
  suppressed path stay untouched.
- `tests/test_session_manager.py` — **modify** — add cases for the
  collision path: (a) AUQ AssistantTurn arrives, then MCP tool call
  arrives → `_pending_questions[tool_use_id].mcp_request_id` is set;
  (b) `send_tool_result(tool_use_id, "Red")` resolves the MCP Future
  with `{"behavior": "deny", "message": "Red"}` and does NOT write a
  user-turn to claude's stdin; (c) MCP-first race — MCP arrives
  before AssistantTurn → request_id parked in deque, then
  AssistantTurn arrives → entry registered with `mcp_request_id` from
  the deque; (d) timeout for an MCP-paired AUQ resolves the Future
  with the canned `"Timed out — no paired user responded within Ns"`
  message (matching the timeout text format used by the MCP server,
  not by `_handle_question_timeout`'s old wording, since the harness
  is now the writer); (e) teardown with an outstanding AUQ:
  `_mcp.cancel_pending(session_id)` drains the Future with deny, and
  `_pending_questions` is cleared.
- `tests/test_bot_ask_user_question.py` — **modify** — add a
  collision test: simulate the realistic sequence (publish
  `AssistantTurn` carrying AUQ tool_use block to bus → simulate MCP
  call → tap a button) and assert ONLY the AUQ keyboard is
  broadcast (no `🛑 Permission requested` message). Validate via
  `bot.send_message.call_args_list` — exactly one outbound call with
  `reply_markup` whose first button's `callback_data` starts with
  `"auq:"`, zero with `"perm:"`. Also assert that resolving the AUQ
  causes `manager.resolve_permission` to fire with the deny+message
  payload (or, equivalently, that the manager's MCP `_futures` map is
  drained — pick the assertion that survives test mocking; see "Test
  surface").
- `tests/test_formatting.py` — **modify** — assert
  `event_to_messages(McpPermissionRequest(tool_name="AskUserQuestion",
  ...))` returns `[]`. Defence in depth.
- `tmp/probe_ccr_028.py` — **artifact** — committed alongside the
  CCR-021/CCR-023/CCR-032 probe scripts so the team can re-run the
  probe if claude's behaviour drifts. The plan does not block the
  developer on touching this file but the artifact stays in `tmp/`.
  (Three captured probe transcripts already exist.)

## Public surface

### `src/ccr/claude/mcp.py` — added surface

```python
# Sketch — illustrative, not the final code.

# Module level (or class attribute on McpPermissionServer):
SuppressedToolHandler = Callable[
    [str, str, dict[str, Any]],  # request_id, tool_name, tool_input
    Awaitable[None],
]


class McpPermissionServer:
    def __init__(
        self,
        *,
        bus: EventBus,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        data_dir: Path | None = None,
        suppressed_tool_names: frozenset[str] = frozenset(),
    ) -> None:
        ...
        self._suppressed_tool_names = suppressed_tool_names
        self._suppressed_handler: SuppressedToolHandler | None = None

    def set_suppressed_tool_handler(
        self,
        handler: SuppressedToolHandler | None,
    ) -> None:
        """Register the callback invoked for suppressed-tool MCP calls.

        Called once at SessionManager construction. Idempotent: passing
        ``None`` clears the handler and the suppressed path falls back
        to the legacy bus-publish behaviour (defence in depth).
        """
        self._suppressed_handler = handler
```

`_on_tool_call` becomes:

```python
# Sketch — illustrative.
async def _on_tool_call(self, tool_name, tool_input):
    request_id = self._mint_request_id()
    future = asyncio.get_running_loop().create_future()
    session_id = self._current_session_id or _PLACEHOLDER_SESSION_ID
    self._futures[request_id] = future
    self._sessions[request_id] = session_id
    self._inputs[request_id] = tool_input

    suppressed = (
        tool_name in self._suppressed_tool_names
        and self._suppressed_handler is not None
    )

    try:
        if suppressed:
            # Notify SessionManager so it can pair the request_id with
            # the right _pending_questions entry. NO bus publish.
            await self._suppressed_handler(request_id, tool_name, tool_input)
        else:
            envelope = McpPermissionRequest(
                request_id=request_id,
                session_id=session_id,
                tool_name=tool_name,
                tool_input=tool_input,
            )
            await self._bus.publish(
                "session.event",
                {"session_id": session_id, "event": envelope},
            )

        try:
            return await asyncio.wait_for(
                future, timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return {
                "behavior": "deny",
                "message": _format_timeout_message(self._timeout_seconds),
            }
    finally:
        self._futures.pop(request_id, None)
        self._sessions.pop(request_id, None)
        self._inputs.pop(request_id, None)
```

Note: `_resolve_internal`'s "auto-fill `updatedInput` from `_inputs`"
path stays — it is still used by the non-suppressed callsite where
the Telegram permission handler does not have the original input. For
suppressed AUQ resolution we ALWAYS set `behavior="deny"`, so that
auto-fill never fires for AUQ. No change to `_resolve_internal`.

### `src/ccr/claude/manager.py` — modified surface

```python
# Sketch — illustrative.

@dataclass(slots=True)
class _PendingQuestion:
    tool_use_id: str
    session_id: uuid.UUID
    options: list[str]
    timeout_task: asyncio.Task[None] | None = None
    mcp_request_id: str | None = None  # NEW (CCR-028)


class SessionManager:
    def __init__(self, *, bus, db_factory, settings) -> None:
        ...
        self._mcp = McpPermissionServer(
            bus=bus,
            timeout_seconds=float(settings.mcp_permission_timeout_seconds),
            data_dir=settings.data_dir,
            suppressed_tool_names=frozenset({"AskUserQuestion"}),
        )
        self._mcp.set_suppressed_tool_handler(self._on_mcp_ask_user_question)
        self._unpaired_mcp_auq_calls: collections.deque[str] = collections.deque()
        ...

    async def _on_mcp_ask_user_question(
        self,
        request_id: str,
        tool_name: str,
        tool_input: dict[str, Any],  # noqa: ARG002 — kept for symmetry
    ) -> None:
        """Pair an incoming AUQ MCP call with a _pending_questions entry.

        Race: the AssistantTurn JSONL line and the MCP socket call
        arrive on different streams. In every observed real-claude
        run the AssistantTurn lands first (claude emits the tool_use
        before invoking the permission tool). FIFO-pair the MCP
        request_id with the FIRST _pending_questions entry whose
        mcp_request_id is None. If no such entry exists, park the
        request_id; _track_ask_user_question consumes from the deque
        on the next AssistantTurn observation.
        """
        for pending in self._pending_questions.values():
            if pending.mcp_request_id is None:
                pending.mcp_request_id = request_id
                return
        self._unpaired_mcp_auq_calls.append(request_id)
```

`_track_ask_user_question` is extended to drain the deque when
registering a new pending question:

```python
# Sketch — illustrative — only the lines added inside the existing
# loop in _track_ask_user_question.
options = _extract_ask_user_question_options(block.input)
mcp_request_id = (
    self._unpaired_mcp_auq_calls.popleft()
    if self._unpaired_mcp_auq_calls
    else None
)
pending = _PendingQuestion(
    tool_use_id=block.id,
    session_id=session_id,
    options=options,
    mcp_request_id=mcp_request_id,
    timeout_task=None,
)
```

`send_tool_result` branches on `mcp_request_id`:

```python
# Sketch — illustrative.
async def send_tool_result(self, tool_use_id, content, *, is_error=False):
    if self._proc is None:
        raise NoActiveSessionError("No active session.")
    pending = self._pending_questions.pop(tool_use_id, None)
    if pending is None:
        return False
    if pending.timeout_task is not None and not pending.timeout_task.done():
        pending.timeout_task.cancel()

    if pending.mcp_request_id is not None:
        # CCR-028: AUQ-via-MCP path. Resolve the Future with deny
        # carrying the answer; claude's harness writes the synthetic
        # tool_result. We never call _deliver_tool_result here — that
        # would produce a SECOND tool_result and race the harness.
        decision = {"behavior": "deny", "message": content}
        return await self._mcp.resolve(pending.mcp_request_id, decision)

    # Legacy / defensive path (no MCP pairing). Keep the wire write so
    # the public API stays useful for callers that may inject a raw
    # tool_result outside the AUQ-MCP collision window.
    try:
        await self._deliver_tool_result(tool_use_id, content, is_error=is_error)
    except RuntimeError as exc:
        log.warning(...)
        return False
    return True
```

Timeout handler symmetry:

```python
# Sketch — illustrative.
async def _handle_question_timeout(self, tool_use_id, deadline):
    try:
        await asyncio.sleep(deadline)
    except asyncio.CancelledError:
        return
    pending = self._pending_questions.pop(tool_use_id, None)
    if pending is None:
        return
    log.warning(...)
    if pending.mcp_request_id is not None:
        decision = {
            "behavior": "deny",
            "message": (
                f"Timed out — no paired user responded within {int(deadline)}s"
            ),
        }
        await self._mcp.resolve(pending.mcp_request_id, decision)
        return
    # Legacy fallback path (kept for the non-MCP write).
    try:
        await self._deliver_tool_result(tool_use_id, message, is_error=True)
    except (RuntimeError, NoActiveSessionError) as exc:
        log.warning(...)
```

`_teardown_locked` does not change — `_mcp.cancel_pending(session_id)`
is already called there and drains every outstanding Future (incl.
AUQ Futures) with the canonical deny payload. Still need to drain
`_unpaired_mcp_auq_calls` (clear the deque) and cancel the per-question
timeout tasks; the latter is already in place from CCR-026.

### `src/ccr/bot/formatting.py` — defensive guard

```python
# Sketch — illustrative.
_AUQ_SUPPRESSED_TOOL_NAMES = frozenset({"AskUserQuestion"})


def _format_mcp_permission(
    event: McpPermissionRequest,
) -> OutboundMessage | None:
    if event.tool_name in _AUQ_SUPPRESSED_TOOL_NAMES:
        # Defence in depth: the MCP server suppresses the bus envelope
        # for these tools, so we should never reach this branch in
        # production. Returning None lets event_to_messages drop the
        # event silently if the suppression at source is bypassed
        # (e.g. test fixture, schema drift).
        return None
    tool = html.escape(event.tool_name)
    args = html.escape(repr(event.tool_input)[:_TOOL_ARG_TRUNCATE])
    text = f"\U0001f6d1 Permission requested\nTool: <code>{tool}</code>\nInput: {args}"
    return (text, _PendingKeyboard(event.request_id, list(event.options)))


def event_to_messages(event):
    if isinstance(event, McpPermissionRequest):
        msg = _format_mcp_permission(event)
        return [msg] if msg is not None else []
    ...
```

(Implementation note for the developer: the cleanest local edit is to
return `None` from `_format_mcp_permission` and unwrap at the
`event_to_messages` callsite as shown. Alternative: keep the function
returning `OutboundMessage` and wrap the suppression check inside
`event_to_messages`. Either is fine; the test asserts on the
`event_to_messages` output.)

## Patterns and prior art

- **Reuse — `McpPermissionServer.resolve(request_id, decision)`:**
  `src/ccr/claude/mcp.py:230-242` already takes a `dict` decision and
  routes it through `_resolve_internal`. The AUQ path uses the
  existing API — no new resolution channel. This is the load-bearing
  pattern.
- **Reuse — `_pending_questions` dict-pop single-shot resolution:**
  `src/ccr/claude/manager.py:604-606` already pops the entry inside
  `send_tool_result`. Add the MCP branch INSIDE the same atomic pop —
  the `pop` is the one and only place we decide who wins a race
  between concurrent button taps / `/answer` / timeout.
- **Reuse — `cancel_pending(session_id)` for teardown:**
  `src/ccr/claude/mcp.py:243-263` already drains every Future for a
  given session with deny. CCR-028 adds NO new teardown logic — the
  existing call in `_teardown_locked` covers AUQ Futures because they
  share the same `_futures` / `_sessions` map.
- **Reuse — discriminated-union `_PendingKeyboard.kind`:** the broadcast
  loop already routes `"auq"` keyboards via `ask_user_question_kb`
  (`src/ccr/server.py:131-152`). No second sentinel kind needed.
- **Reuse — frozenset of tool names parameterisation:**
  `src/ccr/claude/manager.py:72` already uses
  `_SUBAGENT_DISPATCH_TOOL_NAMES: frozenset[str]` for the subagent
  detector. The new `_AUQ_SUPPRESSED_TOOL_NAMES` mirrors that shape.
  CCR-027 adds `"ExitPlanMode"` to the manager's frozenset (and to
  `McpPermissionServer.suppressed_tool_names`) without re-architecting.
- **Reuse — `set_*` configurator on `McpPermissionServer`:**
  `set_current_session(session_id)` (`mcp.py:265`) is the precedent
  for "manager pokes server with a per-session value". The new
  `set_suppressed_tool_handler` follows the same shape.
- **Avoid — keying suppression on `tool_use_id` inside MCP.** The
  `tool_use_id` is NEVER passed to `_on_tool_call`. Trying to surface
  it would require parsing a stream that MCP does not own. FIFO
  pairing in `SessionManager` is the right layer.
- **Avoid — duplicate resolution channels.** `send_tool_result`
  becomes a thin dispatcher (MCP path vs legacy wire path). Do NOT
  add a parallel `resolve_ask_user_question` method on
  `SessionManager`. The public surface stays unchanged.
- **Avoid — content-based MCP/AssistantTurn correlation
  (e.g. hash `tool_input`).** Fragile (claude may add fields, escape
  whitespace, reorder keys). FIFO pairing is correct because AUQ is
  serialised within a session — see "Step-0 probe outcome" point 4.
- **Avoid — auto-allow + send_tool_result.** The probe shows the
  harness writes its own empty fulfilment immediately on `allow`,
  faster than we can inject. Two `tool_result` envelopes for one
  `tool_use_id` is the bug we are fixing, not a fix.
- **Avoid — coupling suppression to `_pending_questions` lifecycle in
  `McpPermissionServer`.** The MCP server stays session-state-free.
  The handler is a callback. The Future is keyed by `request_id` (an
  MCP concept). The bridge to `tool_use_id` lives entirely in
  `SessionManager`.

## Abstractions

**One small extension: `suppressed_tool_names` + handler hook on
`McpPermissionServer`.** The MCP server gains a parameterised
"who-handles-this-tool-call" decision:

- Default behaviour (publish `McpPermissionRequest` to the bus) is
  unchanged for non-suppressed tools.
- Suppressed tools route through a registered handler. The handler is
  optional; if unset, the suppressed-name falls through to the legacy
  bus path (defensive — ensures a misconfigured deployment is no
  worse than today).

**Why this is justified now (not premature):**

- Two concrete callers eventually: `AskUserQuestion` (this ticket),
  `ExitPlanMode` (CCR-027 follow-up). The CLAUDE.md "three callers"
  rule is a soft floor; with one caller landed and one queued the
  alternative is `if tool_name == "AskUserQuestion": ... else: ...`
  inside `_on_tool_call`, which CCR-027 will then have to extend.
  Parameterising on a frozenset is the same line count and removes a
  conditional from the hot path.
- The suppression set on `SessionManager` mirrors
  `_SUBAGENT_DISPATCH_TOOL_NAMES` exactly — same shape, same
  frozenset-of-tool-names spelling. No new vocabulary.
- The handler hook is a one-line setter following the `set_current_session`
  precedent. No new class.

**No new long-lived class.** No `AskUserQuestionGate`, no
`ToolUseRouter`, no `SuppressionRegistry`. Three small additions:

1. `_PendingQuestion.mcp_request_id: str | None` field.
2. `SessionManager._on_mcp_ask_user_question` async method (~10 lines).
3. `SessionManager._unpaired_mcp_auq_calls: deque[str]` for the rare
   MCP-arrives-before-AssistantTurn race.

## Dependencies

- **Depends on:**
  - `src/ccr/claude/mcp.py` — `McpPermissionServer` exists, `_on_tool_call`
    structure stable (CCR-025).
  - `src/ccr/claude/manager.py` — `_pending_questions`,
    `send_tool_result`, `_track_ask_user_question`,
    `_handle_question_timeout`, `_teardown_locked` exist (CCR-026).
  - `src/ccr/claude/events.py` — `McpPermissionRequest` model exists
    (CCR-025).
- **Used by:**
  - CCR-027 (Plan-mode UX) — extends `_AUQ_SUPPRESSED_TOOL_NAMES` with
    `"ExitPlanMode"` (or whatever name the plan-mode tool surfaces
    as) and adds the corresponding pending-state machinery on
    `SessionManager`. Suppression mechanics in `mcp.py` are already
    parameterised; no further `mcp.py` change.

## Edge cases the developer must handle

- **MCP-first race ordering.** Step-0 probe shows AssistantTurn
  always precedes the MCP call in observed real-claude runs, but the
  streams are independent — different fd / different transport
  (stdout vs unix socket). Code must tolerate the reverse: when
  `_on_mcp_ask_user_question` runs and zero pending entries are
  un-paired, the request_id parks in `_unpaired_mcp_auq_calls`. When
  `_track_ask_user_question` next registers an entry, it pops from
  that deque.
- **Multiple AUQ outstanding at once?** The probe shows AUQ is
  serialised — claude emits ONE `tool_use AskUserQuestion`, awaits
  the tool_result, then continues. So `_unpaired_mcp_auq_calls` is
  effectively size-zero or size-one in practice. The deque is
  defensive; FIFO ordering survives even if claude relaxes the
  invariant in a future version.
- **`set_current_session(None)` + a late MCP call.** Possible if the
  session is torn down between `_on_tool_call` minting a future and
  the future actually being resolved — covered by the existing
  `cancel_pending` call in `_teardown_locked`. Test case (e) below
  asserts this.
- **Non-AUQ MCP call AFTER AUQ suppression is active.** Regular
  permission prompts (e.g. for `Bash`, `Edit`) hit the
  `tool_name not in self._suppressed_tool_names` branch and behave
  exactly as today — bus envelope, formatter, keyboard, callback
  resolution. The test surface includes a regression case so this
  cannot drift.
- **Suppression handler raises.** If the handler raises, the Future
  is still in `_futures` and will time out cleanly with the canonical
  deny. Wrap the handler call in `try/except` inside `_on_tool_call`
  and log the exception (`structlog.warning` —
  `"mcp_permission_server.suppressed_handler_failed"`); do NOT re-raise.
  Defensive: a buggy handler must never wedge the MCP loop.
- **`tool_name` arrives empty / unscoped.** The probe confirmed
  `tool_name == "AskUserQuestion"` verbatim. If a future claude
  version scopes it (e.g. `mcp__ccr__AskUserQuestion`), the
  suppression frozenset misses and the bug returns. The unit test
  `test_on_tool_call_publishes_envelope_for_unsuppressed_name`
  pinning the bare name gives an early signal; if the upstream
  changes, surface as a fresh ticket rather than try to anticipate
  the rename.
- **`_pending_questions` already has an entry with non-None
  `mcp_request_id` when the handler runs.** Indicates a duplicate
  MCP call for the same tool_use_id (claude bug or our pairing bug);
  the FIFO loop walks past such entries and either pairs the next
  un-paired entry or parks. Add a `log.warning` if walking past a
  paired entry — surfaces drift.
- **`send_tool_result` with `is_error=True` on an MCP-paired entry.**
  Caller intent is "answer with an error". Current behaviour: the
  legacy path writes `is_error=True` + content. New behaviour for
  the MCP path: there is no `is_error` field on the deny decision —
  claude's harness always writes `is_error=True` for a deny. Treat
  the caller's `is_error` as informational; the wire-level error
  flag is forced by the deny shape. Document this in the docstring
  ("for MCP-paired AskUserQuestion entries, `is_error` is ignored —
  claude's harness flags every deny as is_error=True").
- **Free-text reply with multi-line answer.** The deny `message`
  string is bounded by claude's own max-length on tool_result content
  (no documented cap, but multi-KB strings work in practice). No
  truncation needed in `send_tool_result`; if claude truncates, that
  is an upstream limit not a CCR-028 concern.
- **Permission-prompt timeout configured very short for tests.**
  `_format_timeout_message` already exists in `mcp.py:86`. The
  AUQ-via-MCP timeout uses that same wording; no second timeout
  message string needs maintaining.
- **Defensive bus suppression gates duplicate envelopes from drifting
  in.** Even though `_on_tool_call` skips the publish for suppressed
  names, `_format_mcp_permission` returning `None` for AUQ envelopes
  ensures the ❓ + 🛑 collision is impossible regardless of who
  introduces the envelope (e.g. a future test fixture). Pinning this
  in `tests/test_formatting.py` keeps the invariant from rotting.

## Test surface

### `tests/test_mcp_tool.py` — modify

New cases (do not change existing ones — they pin the non-suppressed
behaviour and must stay green):

- `test_suppressed_tool_call_does_not_publish_bus_envelope`
  — construct `McpPermissionServer(..., suppressed_tool_names=
  frozenset({"AskUserQuestion"}))`; register a stub handler that
  records `(request_id, tool_name, tool_input)`. Drive a tool call
  with `tool_name="AskUserQuestion"` via the in-memory client.
  Assert: the `bus.subscribe("session.event")` queue receives ZERO
  `McpPermissionRequest` envelopes; the handler stub is invoked
  exactly once with the right `tool_name` + `tool_input`; the
  in-flight tool call still blocks on the Future (timeout-tracked).
- `test_suppressed_tool_resolve_unblocks_in_flight_call`
  — same setup; have the stub handler call
  `server.resolve(request_id, {"behavior": "deny", "message": "Red"})`
  inline; assert the in-flight `client.call_tool(...)` returns the
  `{"behavior": "deny", "message": "Red"}` payload (decoded from
  TextContent JSON via `_tool_result_payload`).
- `test_suppressed_tool_timeout_returns_canned_deny`
  — same setup with `timeout_seconds=0.05`; handler does nothing
  (does NOT call `resolve`). Assert the in-flight call returns the
  canonical `_format_timeout_message` deny payload.
- `test_unsuppressed_tool_with_handler_set_still_publishes_envelope`
  — handler is set, but the call uses a non-suppressed
  `tool_name="Bash"`. Assert the bus envelope still publishes; the
  handler is NOT called. Regression for non-AUQ tools.
- `test_suppressed_tool_with_handler_unset_falls_through_to_bus`
  — pass `suppressed_tool_names={"AskUserQuestion"}` but do NOT call
  `set_suppressed_tool_handler`. Assert the bus envelope IS
  published. Defence in depth — ensures a misconfigured deployment
  surfaces broken UX rather than a silent hang.

### `tests/test_session_manager.py` — modify

New cases:

- `test_ask_user_question_pairs_with_mcp_request_id_when_mcp_arrives_after_assistant_turn`
  — register `_pending_questions[tool_use_id]` via the existing
  `_track_ask_user_question` test path (real `AssistantTurn` event);
  invoke `manager._on_mcp_ask_user_question("rid-A",
  "AskUserQuestion", {})`; assert
  `_pending_questions[tool_use_id].mcp_request_id == "rid-A"` and
  `_unpaired_mcp_auq_calls` is empty.
- `test_ask_user_question_pairs_when_mcp_arrives_first`
  — invoke `_on_mcp_ask_user_question("rid-B", ...)` BEFORE any
  AssistantTurn; assert `_unpaired_mcp_auq_calls == ["rid-B"]`.
  Then drive an AssistantTurn carrying an AUQ tool_use; assert the
  new `_pending_questions[tool_use_id].mcp_request_id == "rid-B"`
  and the deque is empty.
- `test_send_tool_result_resolves_mcp_future_with_deny_message`
  — register a question manually with `mcp_request_id="rid-C"`;
  pre-seed an `asyncio.Future` in `manager._mcp._futures["rid-C"]`;
  call `manager.send_tool_result(tool_use_id, "Red")`; assert the
  Future was resolved with `{"behavior": "deny", "message": "Red"}`
  and the entry is gone from `_pending_questions`. Assert NO call
  to `proc.send_user_turn` (the wire path must NOT fire for
  MCP-paired entries).
- `test_send_tool_result_legacy_path_when_no_mcp_pairing`
  — register a question with `mcp_request_id=None`; call
  `send_tool_result`; assert `proc.send_user_turn` IS called with
  the expected `[ToolResultBlock(...)]`. Pins the back-compat path.
- `test_question_timeout_resolves_mcp_future_when_paired`
  — create a question with `mcp_request_id="rid-D"`, very short
  timeout; assert the Future eventually resolves to deny with the
  canonical timeout message; assert NO `proc.send_user_turn` call.
- `test_teardown_with_outstanding_auq_drains_mcp_via_cancel_pending`
  — register a paired AUQ question; pre-seed the MCP Future;
  call `manager.stop()`; assert the Future is resolved with
  `{"behavior": "deny", "message": "Session torn down"}` (the
  string `cancel_pending` already uses); assert
  `_pending_questions` is empty and `_unpaired_mcp_auq_calls` is
  empty.
- `test_unpaired_mcp_auq_calls_cleared_on_teardown`
  — push two ids into `_unpaired_mcp_auq_calls`; call
  `manager.stop()`; assert the deque is empty afterward (whatever
  ids were stored never become real MCP cancellations because no
  Future was registered for them under any active session — they
  are garbage; clearing the deque is the cleanup).
- `test_concurrent_send_tool_result_only_first_winner_resolves_mcp`
  — exists in spirit from CCR-026 (`pop` is single-shot); add an
  assertion that the SECOND call returns `False` AND does NOT call
  `manager._mcp.resolve` a second time.

### `tests/test_bot_ask_user_question.py` — modify

New case for the collision contract (this is the headline ticket
acceptance):

- `test_real_claude_trace_publishes_only_auq_keyboard_no_permission_prompt`
  — using the existing fake-bus + fake-bot harness, simulate the
  real-claude sequence: publish an `AssistantTurn` carrying an AUQ
  `tool_use` block to the bus, then synthesize an
  `_on_mcp_ask_user_question` invocation on the manager (or, more
  realistically, drive the MCP server directly via the in-memory
  client from `tests/test_mcp_tool.py`'s `_client_for` helper — pick
  the path that avoids exercising too many layers in one test). Tap
  one of the keyboard buttons. Assert:
  1. `bot.send_message.call_args_list` contains EXACTLY one call
     whose `reply_markup`'s first button has `callback_data`
     starting with `"auq:"`.
  2. ZERO calls have `callback_data` starting with `"perm:"`.
  3. The MCP Future (or the captured `manager.resolve_permission`
     call, depending on test depth) was resolved with
     `{"behavior": "deny", "message": <chosen option text>}`.
  4. `proc.send_user_turn` was NOT called for this `tool_use_id`
     (the synthetic tool_result is claude's responsibility now).

The developer's call: drive this with the in-memory MCP client (most
realistic) or with a manager-level mock (smaller blast radius).
Either is acceptable as long as all four assertions are made.

### `tests/test_formatting.py` — modify

- `test_event_to_messages_drops_mcp_permission_request_for_ask_user_question`
  — construct
  `McpPermissionRequest(tool_name="AskUserQuestion",
  tool_input={...}, ...)`; assert `event_to_messages(event) == []`.
  Defence in depth — even if MCP suppression is bypassed by a
  fixture, the formatter drops the envelope.
- `test_event_to_messages_keeps_mcp_permission_request_for_other_tools`
  — same setup with `tool_name="Bash"`; assert the formatter still
  returns the keyboard message (regression).

### Coverage gate

`pytest --cov=ccr --cov-fail-under=80` must stay green. The new test
surface adds ~10 cases across three files; the production
modifications are net 30-40 lines on three files. Coverage budget
should remain comfortably above 80% (CCR-026 landed at 88.76%).

## Out of scope

Mirrors the ticket's `Out of scope:` block plus deliberate cuts:

- Other built-in tools that may collide with the MCP gate
  (e.g. `ExitPlanMode`) — file separately if discovered. CCR-027 is
  the slot for plan-mode follow-up.
- Editing CCR-025 / CCR-026 plan files retroactively. Leave the
  archived plans intact — the change-history in `CONTEXT.md` records
  what changed.
- Web-side AskUserQuestion surface. The web viewer continues to render
  the AssistantTurn AUQ tool_use block via its existing event
  formatter; it does not need to know about the MCP suppression.
- A `resolve_for_tool_use_id` method on `McpPermissionServer`. The
  pairing logic lives in `SessionManager` — adding a tool-use-id
  index inside MCP would leak session-level state across the layer.
- Renaming `send_tool_result` to something more accurate
  (e.g. `answer_question`). Public API stability matters more than
  the slight name drift.
- Generic "suppressed tool registry" abstraction. Two-callsite
  parameterisation (frozenset on `McpPermissionServer`, frozenset in
  `formatting.py` defensive guard) is enough.
- Telegram message edit on AUQ timeout. Same deferral as CCR-025 /
  CCR-026.

## Order to land changes

1. Extend `McpPermissionServer.__init__` with `suppressed_tool_names`
   and `set_suppressed_tool_handler`. Branch `_on_tool_call` on the
   suppressed path. Add unit tests in `tests/test_mcp_tool.py`.
2. Add `_PendingQuestion.mcp_request_id` field, the
   `_unpaired_mcp_auq_calls` deque, `_on_mcp_ask_user_question`
   handler, and the `_track_ask_user_question` deque-draining edit
   on `SessionManager`. Wire `set_suppressed_tool_handler` in
   `__init__`. Tests in `tests/test_session_manager.py`.
3. Modify `send_tool_result` and `_handle_question_timeout` to branch
   on `mcp_request_id`. Tests cover both paths.
4. Add the defensive guard in `formatting.py`
   (`_format_mcp_permission` returns `None` for AUQ; `event_to_messages`
   drops). Tests in `tests/test_formatting.py`.
5. Add the end-to-end collision test in
   `tests/test_bot_ask_user_question.py`.
6. Run `pytest --cov=ccr --cov-fail-under=80`, `ruff check`,
   `ruff format --check`, `mypy src`.
7. Manual smoke: `python -m ccr serve` against a real claude session,
   prompt `AskUserQuestion`, tap an option in Telegram, observe that
   only ONE chat message renders (the ❓ keyboard), claude proceeds
   with the chosen answer.

## Decisions

### Resolution shape: `deny + message` (variant `b`-with-message)

The probe ruled out the alternatives:

- **`allow + updatedInput`** (variant `a` with allow): the harness
  ALWAYS emits its own native fulfilment with empty answers
  immediately on allow, faster than we can inject. Two `tool_result`
  envelopes per tool_use_id, claude reports "tool response came back
  empty". The CCR-026 plan's assumption was wrong.
- **`deny`-only with `send_tool_result` injection** (variant `b`
  literal): the deny path skips native fulfilment entirely (we
  confirmed this), but if we ALSO call
  `proc.send_user_turn([ToolResultBlock(...)])` we get TWO
  `tool_result` envelopes for one `tool_use_id` — claude's
  deny-derived one plus ours.
- **Suppress at source + inject our own `tool_result`** (variant `c`
  literal): with suppression at source, the MCP Future never resolves
  → claude blocks indefinitely until the MCP timeout fires (120s
  default), which then writes a deny with the timeout message,
  AFTER we've already injected our answer via `send_user_turn`.
  Two `tool_result` envelopes again, plus a 120s wait if the user
  taps within the AUQ-handler timeout but the MCP Future is
  ignored.

Variant `b`-with-message is the only shape that produces exactly ONE
`tool_result` per `tool_use_id` and proceeds cleanly. We resolve the
MCP Future with `{"behavior": "deny", "message": <answer>}`; the
harness writes the synthetic `tool_result` with `is_error=True,
content=<answer>`; claude reads the answer and continues. The
`is_error=True` is semantically off but does NOT confuse claude in
practice (verified — see Step-0 probe `/tmp/probe_ccr028_real_answer.py`,
captured assistant text described "red" correctly).

### Suppression site: `_on_tool_call` (option ii from the brief)

Suppress the `McpPermissionRequest` bus publish at the source — never
emit the envelope for a suppressed tool name. The alternatives
considered:

- **Suppress in `_format_mcp_permission`** (option i): cleanly
  isolated but loses the chance to decide on resolution shape inside
  MCP. We still need the MCP `_on_tool_call` to delegate to a
  handler instead of awaiting a bus-resolved Future, so we end up
  modifying `_on_tool_call` anyway.
- **Suppress in the broadcast loop in `server.py`** (option iii):
  too late. The SSE consumer would still get the envelope; the web
  viewer would render a phantom permission prompt; the bus would
  carry a duplicated event for a `tool_use_id` that already has an
  AUQ keyboard. Suppression must be at the publisher.

Option ii (suppress at source in `_on_tool_call`) is correct. We
ALSO add the defensive guard in `_format_mcp_permission` (option i)
because:
- it pins the contract at the formatter layer (drift-tolerance);
- it keeps the test surface readable — the formatter test asserts
  the suppression independently of MCP wiring;
- it costs three lines.

### Race ordering: AssistantTurn-first is observed; design tolerates either

In every observed run the AssistantTurn JSONL line lands BEFORE the
MCP `_on_tool_call` invocation — claude emits the `tool_use` block
on stdout, then calls the permission tool over MCP. So
`_track_ask_user_question` reliably runs first and registers
`_pending_questions[tool_use_id]`; the suppressed handler then pairs
the FIFO-first un-paired entry.

The reverse ordering is theoretically possible (different streams,
different transports). The `_unpaired_mcp_auq_calls` deque covers
it: the handler parks the request_id, and
`_track_ask_user_question` consumes from the deque on the next AUQ
observation. In practice the deque is always empty.

Choosing FIFO over content-hash correlation: claude serializes AUQ
calls within a session, so FIFO is unambiguous and survives any
future tool_input schema drift. Content-hash correlation would break
the moment claude reorders a JSON key or escapes a string
differently.

### Coordination interface: callback hook on `McpPermissionServer`

Three options the brief flagged:

- **Callback hook (`set_suppressed_tool_handler`)** — chosen.
  `McpPermissionServer` stays session-state-free; the manager owns
  the pairing logic. Mirrors the existing `set_current_session`
  setter shape (mcp.py:265). Easy to test in isolation —
  `tests/test_mcp_tool.py` registers a stub handler.
- **`resolve_for_tool_use_id` method on `McpPermissionServer`** —
  rejected. Would require the MCP server to maintain a
  `tool_use_id → request_id` map, which means MCP has to know about
  AssistantTurn events, which means leaking JSONL stream
  observation into the MCP layer. The MCP server only sees
  `tool_name + tool_input` per the protocol; teaching it about
  `tool_use_id` widens its surface for one downstream caller.
- **Per-id event subscription inside `_on_tool_call`** — rejected
  for the same reason: requires `_on_tool_call` to wait on a
  manager-owned event keyed on data MCP cannot see (`tool_use_id`).
  The callback-hook is strictly simpler.

The callback signature is `(request_id, tool_name, tool_input)` —
exactly the data MCP has at its disposal.

### Generalisation: `frozenset[str]` parameterisation, AskUserQuestion-only initial value

Per the brief: parameterised, not specific. `McpPermissionServer`
takes `suppressed_tool_names: frozenset[str] = frozenset()`;
`SessionManager` constructs it with `frozenset({"AskUserQuestion"})`.
CCR-027 will:

1. Add `"ExitPlanMode"` (or whatever the actual tool name turns out
   to be — needs its own Step-0 probe per CCR-027's plan) to the
   manager-side frozenset.
2. Wire any plan-mode-specific `_pending_*` machinery on
   `SessionManager` (likely a new method/dataclass — the AUQ
   pairing logic doesn't apply 1:1 because plan-mode emits a
   different shape of "input").
3. NOT modify `mcp.py` — the suppression mechanism is already
   parameterised.

Generalisation boundary: `mcp.py` knows only "this is a suppressed
tool call; delegate to the registered handler". The handler
(`SessionManager._on_mcp_ask_user_question` today; potentially a
dispatcher method tomorrow if we end up with diverging tool-specific
pairing logic) decides how to use the `request_id`. CCR-027 lands
its own pairing handler if AUQ-style FIFO doesn't suit ExitPlanMode.

### `_pending_questions` keeps its current key (`tool_use_id`); MCP `request_id` is a paired sub-field

Two viable schemas:

- Key on `tool_use_id`, sub-field for `mcp_request_id` — chosen.
  Public API (`is_question_pending`, `question_id_by_prefix`,
  `question_options`, `outstanding_free_text_questions`) all stays
  identical. The bot path is unchanged.
- Key on `mcp_request_id`, lookup table for `tool_use_id`. Would
  invert the data model and break CCR-026's accessor surface.

The MCP request_id is an implementation detail of the deny channel;
the bot only ever sees `tool_use_id`. Schema 1 is correct.

### Defensive `_unpaired_mcp_auq_calls` deque

A 1-item edge case in practice — but cheap to maintain and impossible
to wedge. The alternative ("trust observed ordering") would silently
drop an MCP request if a future claude version reorders the streams,
making the AUQ keyboard appear without an MCP Future to resolve. The
deque turns that drift into a "next AUQ observation pairs to the
older request_id" outcome — slightly wrong UX, no hang.

## Open questions for team lead

None that block the developer. The Step-0 probe was run; all three
candidate decisions (allow / deny / block) were observed; the
resolution shape is decided. The plan's "Decisions" section captures
the call points.

If the team-lead reviewing this plan disagrees with any of the
decisions (esp. "deny + message" as the resolution shape), the most
sensitive call to revisit is the `is_error=True` semantic: every
AUQ-resolved tool_result will carry `is_error=True` flag in its JSONL
line, even though the user picked a valid option. Downstream
consumers (e.g. SSE viewer rendering tool_result errors with red
formatting) may want to special-case `is_error=True AND tool_name ==
"AskUserQuestion"` as "not actually an error". That's a CCR-013-level
concern (web viewer formatting) and not in scope here.

## Probe artifacts referenced by this plan

- `tmp/probe_ccr_028.py` — probe driver (this PR; commit alongside
  the production change so future drift can re-run it).
- `tmp/ccr-028-probe-1777969761.{jsonl,tool-calls.jsonl,summary.txt}`
  — `--decision allow` transcript (empty native fulfilment race
  observed).
- `tmp/ccr-028-probe-1777969866.{jsonl,tool-calls.jsonl,summary.txt}`
  — `--decision deny` transcript (single deny-derived tool_result).
- `tmp/ccr-028-probe-1777969959.{jsonl,tool-calls.jsonl,summary.txt}`
  — `--decision block` transcript (claude halts on MCP block,
  AssistantTurn precedes MCP call).
- `/tmp/probe_ccr028_real_answer.py` (transient, not committed) —
  asserts that `deny + message="Red"` is treated by claude as a
  user answer, not as an error to retry.
