# Plan: CCR-022 — Richer `/agents` reply (Running + Library)

## Goal

Lift `/agents` out of the `BLOCKED_INTERACTIVE` canned-reply branch in `passthrough.py` and replace it with a two-section HTML reply: **Running** (subagents the live Claude session has dispatched and not yet collected results for) and **Library** (the `.claude/agents/*.md` files on disk in the project working directory). `/mcp` and `/init` remain untouched. Acceptance criteria from the ticket — both section headers, stable empty-state placeholder, alphabetical Library, HTML-escaping, regression checks on `/mcp` and `/init` — are all covered by this design.

The wire format for "Running" was successfully established from existing repo state (no live probe needed — see "Patterns and prior art / Wire-format evidence" below), so this plan picks **option (a)**: source from `SessionManager` via a new public read-only accessor that tracks subagents from `tool_use` / `tool_result` blocks observed in `_consume_events()`.

## File layout

- `src/ccr/claude/manager.py` — **modify**
  - Add `self._running_subagents: dict[str, str]` to `__init__` (maps `tool_use_id` → `subagent_type`).
  - Reset it (`self._running_subagents = {}`) inside both `new_session()` and `continue_session()` alongside the other per-session bookkeeping resets (next to `self._init_event = asyncio.Event()` and `self._saw_result_success = False`).
  - In `_consume_events()`, after `await log_obj.append(event)` and before `bus.publish(...)` is fine (or right after the existing `isinstance(event, SystemInit)` / `ResultEvent` checks — order doesn't matter), inspect `AssistantTurn` / `UserTurn` content to update the dict.
  - New public method `running_subagents() -> list[str]` returning the snapshot list of `subagent_type` values, **alphabetically sorted**, deduplicated. Returns `[]` if no session is running.
  - In `_teardown_locked()`, clear `self._running_subagents = {}` (defensive; `new_session` reset covers normal flow but this guards the "stop without restart" path).

- `src/ccr/bot/handlers/passthrough.py` — **modify**
  - Add an early-return branch *before* the `BLOCKED_INTERACTIVE` check: `if name == "agents": await _reply_agents(msg, session_manager, settings); return`.
  - Remove `"agents"` from the `BLOCKED_INTERACTIVE` frozenset (so the regression assertion in CCR-010 tests for `/mcp` and `/init` still passes).
  - The handler implementation lives in this same module (see "Handler file decision" below).
  - Inject `Settings` into the handler signature via aiogram workflow data (`dp["settings"] = settings` may need to be added in `app.py` if not already there — see "Dependencies" below).

- `src/ccr/bot/app.py` — **modify** (one-liner)
  - Add `dp["settings"] = settings` in `build_dispatcher()` so handlers can pull it from workflow data. Today the dispatcher only stashes `db_factory` and `session_manager`; this ticket needs `settings.data_dir` for the `.claude/agents/` glob (see "Library source" below).

- `tests/test_passthrough.py` — **modify** (NOT `tests/test_bot_passthrough.py` — that path in the ticket is a typo; the existing file under `tests/` is `test_passthrough.py`).
  - Extend with the test cases listed under "Test surface" below.
  - Imports a new `FakeSettings` helper plus a `tmp_path`-driven `.claude/agents/` directory.
  - Extend `FakeManager` to expose `running_subagents` (returning a list).

- `src/ccr/bot/handlers/agents.py` — **NOT created.** See "Handler file decision".

## Public surface

```python
# Sketch — illustrative, not the final code.

# src/ccr/claude/manager.py
class SessionManager:
    def __init__(self, ...) -> None:
        ...
        # tool_use_id -> subagent_type (e.g. "python-developer")
        # Populated when the Task/Agent tool fires; entry removed when the
        # matching tool_result arrives.
        self._running_subagents: dict[str, str] = {}

    def running_subagents(self) -> list[str]:
        """Return a snapshot of subagent types currently running.

        A subagent is "running" iff a ``tool_use`` block with name in
        :data:`_SUBAGENT_DISPATCH_TOOL_NAMES` has been observed in this
        session and no ``tool_result`` for that ``tool_use_id`` has been
        observed yet. Returns an alphabetically sorted, deduplicated list.
        Returns ``[]`` when no session is running OR no subagents have
        been dispatched (callers cannot distinguish — see edge cases).
        """
        return sorted(set(self._running_subagents.values()))
```

```python
# Sketch — illustrative, not the final code.

# src/ccr/claude/manager.py — _consume_events() additions
from ccr.claude.events import AssistantTurn, UserTurn, ToolUseBlock, ToolResultBlock

# Module constant near the top of manager.py.
# "Task" is the modern name (per SystemInit.tools in 2.1.123 logs);
# "Agent" appears in older session logs. Both are accepted to be
# forward/backward compatible.
_SUBAGENT_DISPATCH_TOOL_NAMES: frozenset[str] = frozenset({"Task", "Agent"})

# Inside _consume_events(), in the per-event loop, after the existing
# SystemInit / ResultEvent isinstance checks:
if isinstance(event, AssistantTurn):
    for block in event.message.content:
        if (
            isinstance(block, ToolUseBlock)
            and block.name in _SUBAGENT_DISPATCH_TOOL_NAMES
        ):
            subagent_type = str(block.input.get("subagent_type") or "").strip()
            if subagent_type:
                self._running_subagents[block.id] = subagent_type
elif isinstance(event, UserTurn):
    content = event.message.content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, ToolResultBlock):
                self._running_subagents.pop(block.tool_use_id, None)
```

```python
# Sketch — illustrative, not the final code.

# src/ccr/bot/handlers/passthrough.py
import html
from pathlib import Path

_AGENTS_LIBRARY_GLOB_REL = ".claude/agents"
_EMPTY_PLACEHOLDER = "(none)"


def _list_library_agents(project_root: Path) -> list[str]:
    """Return alphabetically sorted .md filenames (without extension) under .claude/agents/.

    Returns ``[]`` if the directory does not exist. Hidden files (leading
    ``.``) and non-``.md`` files are skipped. Names are NOT HTML-escaped here;
    that happens at render time.
    """
    agents_dir = project_root / _AGENTS_LIBRARY_GLOB_REL
    if not agents_dir.is_dir():
        return []
    names = [p.stem for p in agents_dir.glob("*.md") if p.is_file() and not p.name.startswith(".")]
    return sorted(names)


def _render_agents_reply(running: list[str], library: list[str]) -> str:
    def _section(title: str, items: list[str]) -> str:
        if not items:
            body = _EMPTY_PLACEHOLDER
        else:
            body = "\n".join(f"• {html.escape(name)}" for name in items)
        return f"<b>{title}</b>\n{body}"

    return f"{_section('Running', running)}\n\n{_section('Library', library)}"


async def _reply_agents(
    msg: Message,
    session_manager: SessionManager,
    settings: Settings,
) -> None:
    running = session_manager.running_subagents()
    library = _list_library_agents(settings.data_dir.parent)
    text = _render_agents_reply(running, library)
    # No chunking needed in practice (cap is ~3500 chars; library lists are
    # tiny). Belt-and-suspenders: if it ever exceeds SAFE_CHUNK we let
    # Telegram reject it rather than splitting mid-section. See edge cases.
    await msg.answer(text)
```

```python
# Sketch — illustrative, not the final code.

# src/ccr/bot/handlers/passthrough.py — updated dispatch
async def cmd_passthrough(
    msg: Message,
    command: CommandObject,
    session_manager: SessionManager,
    settings: Settings,
) -> None:
    name = command.command
    args = command.args or ""

    if name == "agents":
        await _reply_agents(msg, session_manager, settings)
        return

    if name in WHITELIST:
        ...
    if name in BLOCKED_INTERACTIVE:  # now {"mcp", "init"}
        ...
    await msg.answer(_UNKNOWN_USAGE_HINT)
```

## Patterns and prior art

### Wire-format evidence (the load-bearing finding)

Real Claude session logs in `data/logs/` already capture multi-subagent runs. Three pieces of evidence that establish option (a) is viable:

1. **`SystemInit.tools` carries `"Task"`** as a tool name in v2.1.123 sessions:
   `data/logs/3a4dcdef-82d2-488a-832f-c86f57338ba1.jsonl:1` — the `tools` array starts with `["Task","AskUserQuestion","Bash","CronCreate",…]`. `Task` is the dispatcher tool, not a subagent name.

2. **`tool_use` blocks with `name == "Agent"` and `input.subagent_type`** appear in older sessions in the same directory:
   `data/logs/3a4dcdef-82d2-488a-832f-c86f57338ba1.jsonl` contains a block:
   ```json
   {"type":"tool_use","id":"toolu_019NKwAGwPbgsNrWATKqaaBK","name":"Agent",
    "input":{"description":"Extend /continue with id support",
             "subagent_type":"python-developer","prompt":"…"}}
   ```
   The matching `ToolResultBlock` has `tool_use_id == "toolu_019NKwAGwPbgsNrWATKqaaBK"` (`is_error: False`). Other agent dispatches in the same log carried `subagent_type` values like `"general-purpose"` and `"architect"`.

3. **`SystemInit.agents`** carries a richer Library hint than the disk glob (`["architect","Explore","general-purpose","Plan","project-manager","python-developer","qa","reviewer","statusline-setup","team-lead","web-developer"]`) — but the ticket explicitly scopes "Library" to `.claude/agents/*.md` and forbids alternative sources (Out of scope: "Globbing agent libraries outside `.claude/agents/`"). We do not use this field; mentioned only because future tickets might want it.

The probe transcript at `tmp/ccr-021-probe-1777590778.jsonl` does NOT contain agent dispatches (it was a single-prompt permission probe), so the data above (`data/logs/3a4dcdef-…`) is the canonical source.

**Conclusion**: option (a) is implementable today. Both legacy `"Agent"` and current `"Task"` tool names are accepted via `_SUBAGENT_DISPATCH_TOOL_NAMES = frozenset({"Task", "Agent"})` so the design is forward- and backward-compatible. The `subagent_type` input field is the source of truth for the rendered name; we render it verbatim (HTML-escaped).

### Patterns

- **Reuse**: `src/ccr/claude/manager.py:518-552` — `_consume_events()` already inspects events with `isinstance(event, SystemInit)` and `isinstance(event, ResultEvent)`. Adding two more `isinstance` arms (one for `AssistantTurn`, one for `UserTurn`) follows the same pattern; no new dispatch surface.
- **Reuse**: `src/ccr/bot/formatting.py:128-156` — content-block iteration over `event.message.content` already inspects `ToolUseBlock`/`ToolResultBlock`. The manager's tracker uses the same idiom.
- **Reuse**: `src/ccr/bot/handlers/session.py:228-254` — `cmd_sessions` already builds a multi-line HTML reply with HTML-escaped fields and `chunk_text` chunking; this is the prior-art template for `/agents` rendering. We deliberately do **not** chunk here (see "Edge cases") — `/agents` output is bounded by `len(.claude/agents/*.md)` plus a handful of subagents, both far below 3500 chars in any realistic scenario.
- **Reuse**: `html.escape()` (used everywhere in `formatting.py`, `session.py`, etc.) — applied to every interpolated agent name for HTML mode safety.
- **Reuse**: `src/ccr/bot/app.py:43-46` workflow-data stash pattern — extend with `dp["settings"] = settings` to make `Settings` available to the handler. aiogram resolves `settings: Settings` parameters by name from workflow data the same way it does for `session_manager` and `db_factory`.
- **Avoid**: a new abstraction in `formatting.py` for "render a section". The two-section render is bounded and trivially inlined; abstracting now would be premature (CLAUDE.md: "default to no new abstraction unless three concrete callers exist"). CCR-030 (`/skills` — single-section) and a hypothetical follow-up could share a helper later, but two callers is not three.
- **Avoid**: parsing YAML frontmatter inside the `.md` files for a `name:` field. The plan note in BACKLOG.md mentions this as an option ("the YAML frontmatter `name:` if present, mirroring how `dev-stack-agents` formats them under `templates/`"), but: (1) it adds a YAML-parsing dependency to the bot module, (2) the filename is the canonical identifier already (`.claude/agents/python-developer.md` → `python-developer`), and (3) the ticket's acceptance criterion 3 explicitly says "the Library section lists `foo` and `bar`" given files `foo.md`/`bar.md` — i.e. filename-stem is the ticket's expected display name. Filename-stem only.

## (a) / (b) / (c) decision

**Option (a) — source from `SessionManager`.** Selected.

Rationale: the wire format is established from existing repo state (see above). `Task`/`Agent` tool names with `subagent_type` input give us a clean start signal; matching `tool_use_id` on `ToolResultBlock` gives us a clean stop signal. The accessor is a one-line `sorted(set(self._running_subagents.values()))`. The tracking lives where similar bookkeeping (`_init_event`, `_saw_result_success`) already lives.

Why not (c) hybrid placeholder: option (a) is implementable now, has tests-as-data evidence, and produces the strictly better UX. The placeholder string `"(unknown — not exposed by claude -p)"` would be technically incorrect — `claude -p` *does* expose subagent dispatch on stdout; we just hadn't read the JSONL closely enough.

Why not (b) drop "Running": the ticket's Acceptance criterion 1 explicitly requires a `"Running"` section header. Dropping it fails acceptance.

### What "running" means

A subagent is "running" iff its `Task`/`Agent` `ToolUseBlock` was observed and the matching `ToolResultBlock.tool_use_id` has not been observed yet. Concretely:

- A subagent that returned an error (`is_error=true` on the result) is **not** running — we remove on any matching `tool_result`, error or not. (Claude's stream-json model sends a `tool_result` for every dispatched tool, errors included.)
- A subagent dispatched on a previous session (`new_session()` reset wipes `_running_subagents`) is **not** running.
- `running_subagents()` does not "look ahead" to a session's JSONL log on disk — it only reflects events already observed in the current process. This is consistent with how `_init_event` and `_saw_result_success` work.
- On a stale session (`session_manager.status() == STOPPED`), `running_subagents()` returns `[]` because `_teardown_locked()` clears the dict.
- On a session that never dispatched a subagent, `running_subagents()` returns `[]` (empty state → `(none)` in the reply).

## Handler file decision

**Keep the implementation in `passthrough.py` — do NOT create `src/ccr/bot/handlers/agents.py`.**

Rationale:
- The added code is ~30 lines (one branch in `cmd_passthrough`, two helpers `_list_library_agents` and `_render_agents_reply`, a small async `_reply_agents`). This does not exceed the threshold for splitting modules per the ticket's "developer's call" language.
- Splitting would introduce a new aiogram `Router` that has to be registered alongside `passthrough_router` in `app.py` — that's a new wiring concern with no offsetting benefit.
- The branch decision (`name == "agents"`) is co-located with the `BLOCKED_INTERACTIVE`/`WHITELIST` decisions, which is exactly the existing structure. Future tickets (`/skills` per CCR-030) follow the same in-module branch pattern, then we extract once we have three callers.

The ticket's `Files:` block lists `agents.py` as **optional, developer's call**. We choose not to.

## Library source

`Path(settings.data_dir).parent / ".claude" / "agents"`.

- `settings.data_dir` defaults to `Path("./data")` (relative to cwd; per `src/ccr/config.py:58`).
- The project root is `data_dir.parent` — i.e. wherever `python -m ccr serve` was invoked from. This is the ticket's "the project working directory" (Notes: "glob `.claude/agents/*.md` from the project working directory").
- Glob: `agents_dir.glob("*.md")` (one segment, no recursion). Hidden files (`.foo.md`) and directories (`*.md/` — pathological) are filtered with `p.is_file() and not p.name.startswith(".")`.
- Display name: `Path.stem` of each match (filename without `.md`). Sorted alphabetically before rendering.
- Empty / missing directory: returns `[]`, which renders as `(none)` under the Library header.
- Symlinks: followed (default `pathlib.Path.glob` behaviour). Out of scope to harden.

We deliberately use `data_dir.parent` rather than a brand-new settings field, because:
1. `data_dir` is already an absolute or cwd-relative `Path` whose parent is the project root by construction (the install layout always puts `data/` at the project root — see `install.sh` plan in CCR-017 and `mkdir -p data/logs` in plan §8 Phase 14 task 1).
2. Adding a `project_root` settings field would touch `src/ccr/config.py` and `tests/test_config.py` for a one-call need; reusing `data_dir.parent` is a cheap derivation, consistent with how the rest of the codebase handles project-relative paths.

## Reply format

HTML mode (matches the rest of the bot's `parse_mode=ParseMode.HTML`).

```
<b>Running</b>
• python-developer
• architect

<b>Library</b>
• architect
• project-manager
• python-developer
```

- Each section opens with `<b>{title}</b>` on its own line.
- Each name is rendered as `• {html.escape(name)}` on its own line.
- Empty section: the literal string `(none)` (parens included, no escaping needed). Stable across calls.
- Sections separated by `\n\n` (one blank line).
- Sort order: alphabetical for both sections (the ticket allows "developer's call but must be stable across calls — alphabetical recommended").
- No chunking. The reply size is bounded by `len(running) + len(library)` × ~30 chars per line, both well under 1 KB in any realistic configuration. We do not call `chunk_text()`. If a user has 1000+ files in `.claude/agents/`, they'll hit Telegram's 4096-char limit and the bot will raise — that's acceptable behaviour for this ticket (out of scope to engineer for; a follow-up could chunk if it ever matters).

## Abstractions

**No new abstraction.** Two helpers (`_list_library_agents`, `_render_agents_reply`) live as module-private functions in `passthrough.py`. The render logic is inlined (a 5-line `_section` closure inside `_render_agents_reply`); we do not extract a "section renderer" to `formatting.py` because:
- Only one caller (this handler) needs it today.
- CCR-030 (`/skills`) is a hypothetical second caller, but it's a single-section reply with a different shape (one section, possibly different empty-state semantics for "no active session" vs "library empty"). Premature to share.
- Per CLAUDE.md: "Default to no new abstraction unless the ticket already produces three concrete callers."

The `SessionManager.running_subagents()` accessor is a new public method, but it's the smallest possible surface (returns a `list[str]`, no parameters, idempotent). Not an abstraction so much as a read-only snapshot, mirroring `current_session_id` and `current_log` already on the class.

## Dependencies

- **Depends on:**
  - `src/ccr/claude/events.py` — `AssistantTurn`, `UserTurn`, `ToolUseBlock`, `ToolResultBlock` (already exported via `__all__`).
  - `src/ccr/claude/manager.py` — `_consume_events()` event loop.
  - `src/ccr/config.py` — `Settings.data_dir` (already exposed; no schema change).
  - `src/ccr/bot/app.py` — workflow-data stash pattern; one new key (`settings`).
  - aiogram workflow-data injection by name (existing pattern).

- **Used by:**
  - Future CCR-030 (`/skills`) may reuse the section-rendering shape but should NOT be blocked on extracting a helper from this ticket.
  - The web viewer (CCR-013) and any future per-subagent UI are explicitly out of scope.

- **Settings keys touched:** none added. `settings.data_dir` is read-only here.
- **DB tables:** none.
- **EventBus topics:** none added. `running_subagents()` reads in-memory state populated by the existing `session.event` consumer; it does not subscribe.

## Edge cases the developer must handle

1. **`AssistantTurn` with non-`Task`/`Agent` tool names.** Most tool calls (`Bash`, `Edit`, `Read`, etc.) are NOT subagent dispatches. The `_SUBAGENT_DISPATCH_TOOL_NAMES` frozenset gate is what prevents `running_subagents()` from listing every running shell command.

2. **Missing `subagent_type` in `tool_use.input`.** Some `Task`/`Agent` calls might omit `subagent_type` (forward-compat / unknown variant). Skip silently — do not insert the entry. The tool_use_id will then never appear in the dict, and the matching tool_result's `pop(...,  None)` is harmless.

3. **Concurrent subagent dispatches.** Claude can dispatch multiple subagents in one turn (the `3a4dcdef-…` log shows this). Each `tool_use_id` is unique so the dict tolerates concurrency naturally. `running_subagents()` returns a deduplicated `set` cast to a sorted list, so two simultaneous `python-developer` dispatches show as one entry. (We could change this to "show count" — `python-developer (×2)` — but that's UX gold-plating outside the ticket's acceptance criteria. Skip.)

4. **`UserTurn.message.content` is a `str`, not a `list`.** The `_UserMessage` model in events.py types this as `str | list[ContentBlock]`. The tracker code must check `isinstance(content, list)` before iterating — single-string user turns are user-typed prompts, never carry tool_results.

5. **Stale session after `/stop` then `/agents`.** `_teardown_locked()` runs `self._running_subagents = {}` (defensive clear) so a fresh `/agents` between sessions returns an empty Running section. **Acceptance criterion 2** (no library + no running → both placeholders) is verified by this path.

6. **No `Settings` in workflow data on test paths.** Tests today inject only `session_manager` and `db_factory`; the new `settings` parameter requires either (a) updating tests to inject a `FakeSettings`, or (b) using `dp["settings"] = settings` consistently in `app.py` and matching it in tests. Choose (a)+(b): always set `dp["settings"]`, always inject in tests. Existing tests in `tests/test_passthrough.py` call `cmd_passthrough(...)` directly with kwargs — they continue to work; only the new `/agents` tests need to pass `settings=fake_settings`.

7. **`.claude/agents/` does not exist.** Common — most projects don't have it. Return `[]` from `_list_library_agents`. Library section renders as `(none)`.

8. **`.claude/agents/` is not a directory (e.g. user created a regular file at that path).** `agents_dir.is_dir()` short-circuits to `[]`. Acceptable.

9. **HTML-escape coverage.** Filename-stems with `<`, `>`, `&` are pathological but possible (filesystem allows them). `html.escape(name)` at render time covers the acceptance criterion.

10. **`/agents` is currently rendered as `🔧 Agent {short_args}` in the live event broadcast** (because `formatting.py:_format_tool_use` already turns `ToolUseBlock(name="Agent", ...)` into a Telegram message). This is **unchanged by this ticket**. The user sees the live tool-use line as a session event AND can call `/agents` to get the snapshot. No double-rendering, no de-duplication needed; the two surfaces are independent.

11. **`new_session()` waiting for init: race with subagent dispatch.** The `_running_subagents` reset in `new_session()` runs *before* the consumer task is created (lines 210, 225 in current `manager.py`), so the first event the consumer ever sees has a fresh dict. No race.

## Test surface

All in `tests/test_passthrough.py` (existing file). Note: ticket says `tests/test_bot_passthrough.py` — that file does not exist; the actual file is `test_passthrough.py`. Developer should **not** create a new `test_bot_passthrough.py`; extend the existing module.

The `FakeManager` stub gains a `running_subagents` method (returning a configurable list) alongside its existing `send_slash`. A new module-level `_make_fake_settings(tmp_path)` helper builds a `Settings` whose `data_dir` is `tmp_path / "data"`, so `_list_library_agents` looks at `tmp_path / ".claude/agents/"`.

Test cases:

- `tests/test_passthrough.py::test_agents_renders_running_and_library_sections` — given a `FakeManager` returning `["python-developer", "architect"]` and two `.md` files in the tmp `.claude/agents/`, the reply contains both `<b>Running</b>` and `<b>Library</b>`, both lists are alphabetical, and both bullet points (`•`) appear. (Acceptance 1, 3.)

- `tests/test_passthrough.py::test_agents_empty_running_and_empty_library` — `FakeManager.running_subagents = []` and an empty / missing `.claude/agents/` dir. Both sections render with the literal `(none)` placeholder under their headers. (Acceptance 2.)

- `tests/test_passthrough.py::test_agents_only_library_populated` — `FakeManager.running_subagents = []` but two library files. Running shows `(none)`, Library lists both files. (Acceptance regression / sanity.)

- `tests/test_passthrough.py::test_agents_only_running_populated` — `FakeManager.running_subagents = ["python-developer"]`, no library dir. Library shows `(none)`, Running lists the subagent.

- `tests/test_passthrough.py::test_agents_html_escapes_name_special_chars` — library file named `<weird>.md`; running list contains `"a&b"`. Reply contains `&lt;weird&gt;` and `a&amp;b`, never the raw characters. (Acceptance HTML-escape criterion.)

- `tests/test_passthrough.py::test_agents_no_longer_returns_blocked_interactive_canned_string` — `FakeManager.running_subagents = []`, `cmd_passthrough` is called with `_command("agents")`. Asserts the reply does NOT contain the literal `"Interactive command — run /agents in your local Claude Code terminal."` substring. (Acceptance 4 — regression check on `BLOCKED_INTERACTIVE` removal.)

- `tests/test_passthrough.py::test_mcp_still_blocked_interactive` — already exists (`test_mcp_is_blocked`); confirm it still asserts the canned `/mcp` reply. (Acceptance 5.)

- `tests/test_passthrough.py::test_init_still_blocked_interactive` — **new**: parallel to the existing `/mcp` test. Asserts `/init` returns the canned `"Interactive command — run /init in your local Claude Code terminal."` reply. (Acceptance 5 — `/init` regression coverage was missing in CCR-010 and should be added now since this ticket touches the same set.)

- `tests/test_passthrough.py::test_agents_library_sorted_alphabetically` — given files `zeta.md`, `alpha.md`, `mike.md`, the rendered Library lists them in alphabetical order. (Acceptance 3 — stable sort.)

- `tests/test_passthrough.py::test_agents_library_skips_non_md_and_hidden` — `.claude/agents/` contains `foo.md`, `bar.txt`, `.hidden.md`, `subdir/`. Only `foo` appears. (Edge case 7-8.)

For the `SessionManager.running_subagents()` accessor itself, add **two** unit tests in `tests/test_session_manager.py` (existing file — used by CCR-009, CCR-019, CCR-020):

- `tests/test_session_manager.py::test_running_subagents_tracks_agent_tool_use_and_clears_on_result` — drive a fake-claude session that emits an `assistant` turn with a `Task` tool_use (subagent_type=`"python-developer"`), assert `running_subagents() == ["python-developer"]`; then emit a `user` turn with a matching `tool_result`, assert `running_subagents() == []`.

- `tests/test_session_manager.py::test_running_subagents_ignores_non_subagent_tools` — drive a session that emits an `assistant` turn with a `Bash` tool_use; assert `running_subagents() == []`. Then emit an `Agent` tool_use with `subagent_type="architect"`; assert `running_subagents() == ["architect"]`. Confirms the `_SUBAGENT_DISPATCH_TOOL_NAMES` filter and the alternate `"Agent"` name.

The `tests/fakes/fake_claude.py` may need a fixture line emitting a `Task`/`Agent` tool_use; likely it can be parameterised at the test site without changing the fake binary itself. Developer's call.

## Out of scope

(Mirroring the ticket's `Out of scope:` block, plus additions.)

- Adding a `/subagents` command or per-subagent web-viewer tab.
- Editing / creating agent library files from Telegram.
- Changing the reply for `/mcp` or `/init`.
- Globbing agent libraries outside `.claude/agents/` (no `~/.claude/agents/`, no project-tree walking).
- Parsing YAML frontmatter in `.md` files for a custom display name. Filename-stem only.
- Showing dispatch counts (`python-developer (×3)`). Single deduplicated list.
- Showing in-flight subagent IDs / descriptions / prompts. Just the type name.
- Sourcing Library from `SystemInit.agents` (richer, but the ticket scopes Library to disk).
- Adding a new `project_root` settings field. Use `data_dir.parent`.
- Chunking the reply via `chunk_text` — the bounded size makes this unnecessary; revisit only if a user actually trips Telegram's hard limit.
- Test data for `tests/fakes/fake_claude.py` if the existing flow lets us drive `_consume_events` with synthetic events directly. Developer's call; either approach is acceptable.
- Touching `tests/test_bot_passthrough.py` — that file does not exist. Developer must extend `tests/test_passthrough.py`.

## Open questions for team lead

None. The wire-format question that triggered the architect dispatch (option a/b/c) is resolved by the existing JSONL evidence in `data/logs/`. The handler-file decision and the Library glob path are settled. No scope smuggling. Plan is implementation-ready.
