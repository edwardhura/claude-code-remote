# Plan: CCR-REFACTOR-AUDIT — codebase cleanup pass

## Verdict

**The codebase is in good shape.** The most recent agent-driven tickets (CCR-024, CCR-025, CCR-028, CCR-029) already collapsed the largest pieces of dead code as they shipped (CCR-009 permission JSONL gate, the post-CCR-021 schema reservation, etc.). What is left is a small set of low-risk, locally-scoped cleanup items — none of them are blockers, none of them are subtly incorrect, and none of them require new design work. **A single small `CCR-REFACTOR` ticket is enough**; bundling more would be padding.

If the team is also fine deferring these until they bite, the audit's recommendation is "no work needed right now". The findings below are the genuine ones; the rejected list at the bottom is what was checked and decided against.

---

## Findings

### 1. Dead `claude_session_id` reservation in `_db_lookup_*` return tuples

**What.** `SessionManager._db_lookup_most_recent_finished` and `_db_lookup_session_by_prefix` return a 2-tuple `(uuid.UUID | None, str | None)` where the second element is reserved for "Outcome 2 `claude_session_id` capture". `Session` has no `claude_session_id` column; both methods read it via `getattr(row, "claude_session_id", None)`, which is **always `None`**. Both call sites destructure it into `_prior_claude_id` and discard.

**Where.**
- `/Users/edward/workspace/claude-code-remote/src/ccr/claude/manager.py:439` — `prior_id, _prior_claude_id = await self._db_lookup_most_recent_finished()`
- `/Users/edward/workspace/claude-code-remote/src/ccr/claude/manager.py:444-446` — `prior_id, _prior_claude_id = await self._db_lookup_session_by_prefix(...)`
- `/Users/edward/workspace/claude-code-remote/src/ccr/claude/manager.py:1138-1158` — `_db_lookup_most_recent_finished` body and docstring
- `/Users/edward/workspace/claude-code-remote/src/ccr/claude/manager.py:1160-1187` — `_db_lookup_session_by_prefix` body and docstring
- `/Users/edward/workspace/claude-code-remote/src/ccr/db/models.py:91-118` — `Session` model with no such column
- `/Users/edward/workspace/claude-code-remote/alembic/versions/0001_initial.py:54-78` — migration confirms column was never created

**Why it matters now.** It is the most concrete piece of "code that exists in case a future ticket needs it" in the codebase, and the reservation is semantically misleading: the Outcome 2 path was settled when CCR-020 landed Outcome 1 (`--continue` works without a stored session id). The next reader of `manager.py` has to reconstruct the same probe context to decide whether the slot is reachable. Per CLAUDE.md: don't keep backwards-compat / future-compat shims around.

**Fix scope.** Single-file change. Drop the second tuple element; both helpers return `uuid.UUID | None`. Update both call sites and both docstrings. No new abstraction.

**Risk / blast radius.** Tiny. Only two call sites, both internal; no public API touched; no DB / wire-format change; no test updates needed beyond rerunning the suite.

---

### 2. Unused exception classes `StaleSessionError` / `StaleToolUseError`

**What.** Both classes are defined, exported in `__all__`, and re-exported from `ccr.claude.__init__`. Neither is `raise`d anywhere in the project, neither is `except`-ed anywhere, and the docstring on `StaleToolUseError` (`manager.py:172-179`) explicitly admits it is "Reserved for internal assertions and future callers."

**Where.**
- `/Users/edward/workspace/claude-code-remote/src/ccr/claude/manager.py:168-170` — `class StaleSessionError(SessionError)`
- `/Users/edward/workspace/claude-code-remote/src/ccr/claude/manager.py:172-179` — `class StaleToolUseError(SessionError)`
- `/Users/edward/workspace/claude-code-remote/src/ccr/claude/manager.py:1288-1289` — exported in `__all__`
- `/Users/edward/workspace/claude-code-remote/src/ccr/claude/__init__.py:26-27,43-44` — re-export

**Why it matters now.** `StaleToolUseError` was reserved against a CCR-026 design decision — the docstring says `send_tool_result` was supposed to maybe raise it but actually returns `False`. Same shape as the `_prior_claude_id` reservation: dead future-compat scaffolding that confuses the reader.

**Fix scope.** Same ticket as finding 1 — single-file deletion plus `__all__` edits in two files (`manager.py` and `claude/__init__.py`). No callers.

**Risk / blast radius.** Tiny. No external library could be importing these (the project is not yet released).

---

### 3. `permission_kb` `_LABELS` map carries dead `"skip"` / `"abort"` keys; matching test exercises an unreachable combo

**What.** `_LABELS` in `keyboards.py` maps `"approve"`, `"skip"`, `"abort"` to display strings. In production the only `options` value plumbed through is the default on `McpPermissionRequest.options` — `["approve", "deny"]` — and `_ALLOWED_CHOICES` in the handler whitelists exactly `{"approve", "deny"}`. So `"skip"` and `"abort"` keys are unreachable, and `"deny"` is missing from the map (it falls back to `opt.capitalize()` → `"Deny"`).

**Where.**
- `/Users/edward/workspace/claude-code-remote/src/ccr/bot/keyboards.py:26-30` — `_LABELS` map with stale keys
- `/Users/edward/workspace/claude-code-remote/src/ccr/claude/events.py:287` — `options: list[str] = Field(default_factory=lambda: ["approve", "deny"])` — the only producer
- `/Users/edward/workspace/claude-code-remote/src/ccr/claude/mcp.py:416-421` — only constructor of `McpPermissionRequest`; never overrides `options`
- `/Users/edward/workspace/claude-code-remote/src/ccr/bot/handlers/permission.py:38` — `_ALLOWED_CHOICES = frozenset({"approve", "deny"})`
- `/Users/edward/workspace/claude-code-remote/tests/test_bot_permission.py:58-66` — `test_keyboard_buttons_match_options_and_callback_data` builds a keyboard with `["approve", "skip", "abort"]` — combo unreachable in production; the handler would reject `"skip"` / `"abort"` taps as "Stale prompt"

**Why it matters now.** This is the canonical "test drifted from the code it covers" finding. The keyboard test still passes because `permission_kb` accepts any list, but the test asserts behaviour for inputs that no real producer can generate. A new contributor reading `keyboards.py` will reasonably assume that `"skip"` / `"abort"` are part of the permission flow and waste time looking for the producer. The label map for `"deny"` is also asymmetric — the only valid choice that does *not* have a friendly label.

**Fix scope.** Same ticket. Three small touches:
1. `keyboards.py` `_LABELS`: drop `"skip"` / `"abort"`, add `"deny": "Deny"`.
2. `tests/test_bot_permission.py:58-66`: rewrite the test to use `["approve", "deny"]` so it pins the real production combo.
3. Optionally — only if the architect thinks the simplification is worth it — drop `McpPermissionRequest.options` entirely (always `["approve", "deny"]`) and inline the literal in `permission_kb`. **Recommend NOT doing this**: the field is one-line, harmless, and provides a future seam for "approve once / approve always" if that is ever wanted. Touching it widens blast radius without benefit. Leave `options` in place; just clean the labels.

**Risk / blast radius.** Tiny. The `_LABELS` change alters one human-visible string (the friendly "Deny" label vs. the current `"Deny"` from `capitalize()` — same output, no UX delta). Test rewrite stays within one file.

---

### 4. Stale tense in `keyboards.py` and `server.py` module docstrings

**What.** Two module docstrings still describe shipped tickets in future tense.

**Where.**
- `/Users/edward/workspace/claude-code-remote/src/ccr/bot/keyboards.py:1-10` — "The function is kept dormant after CCR-024 stripped the dead JSONL-based permission channel; **CCR-025 will reuse it** for the MCP-driven envelope. **CCR-014 (`/view` / `/last` / `/preview`) will land link buttons here as well**, which is why this lives in its own module rather than getting merged into `formatting.py`." CCR-025 has shipped (and reuses the function); CCR-014 is still open, so the second sentence is fine.
- `/Users/edward/workspace/claude-code-remote/src/ccr/server.py:1-7` — "Uvicorn / web server lands in **CCR-012 and will join the same `asyncio.gather`**." CCR-012 is still open, so this is technically accurate, but the docstring also misnames the orchestration (it is `asyncio.wait(..., FIRST_COMPLETED)`, not `gather`). The plan-language drift is small but the inaccurate primitive name is a real reading-trap when grepping.

**Why it matters now.** Doc drift is the class of finding most likely to mislead the next contributor for free. The CCR-012 reference itself can stay as a forward pointer; the `asyncio.gather` mention should be reconciled to `asyncio.wait`.

**Fix scope.** Same ticket, two-line edits. Update `keyboards.py` to past-tense for the CCR-024/CCR-025 sentence; update `server.py` to say `asyncio.wait` (or simply drop the implementation-detail half of that sentence).

**Risk / blast radius.** None — comments only.

---

### 5. `claude/log.py::prune` is exported and tested but never wired into startup

**What.** `prune(logs_dir, retention_count, retention_days)` exists, has 3 tests in `tests/test_claude_log.py`, and is named in `__all__` of `ccr.claude.log`. No call site exists anywhere under `src/`. The plan (`claude-code-remote-plan.md` §706) specifies it for "startup cleanup" and the `Settings` carries `log_retention_count` / `log_retention_days` for exactly this use case — both also unused by any source file.

**Where.**
- `/Users/edward/workspace/claude-code-remote/src/ccr/claude/log.py:149-205` — `prune` definition
- `/Users/edward/workspace/claude-code-remote/src/ccr/claude/log.py:208` — exported in `__all__`
- `/Users/edward/workspace/claude-code-remote/src/ccr/config.py:61-62` — `log_retention_count` / `log_retention_days` settings
- `/Users/edward/workspace/claude-code-remote/src/ccr/server.py:42-89` — `serve()` startup; calls `manager.reconcile_orphans()` but never `prune`
- `/Users/edward/workspace/claude-code-remote/tests/test_claude_log.py:167,186,194` — direct unit tests of `prune` (no integration coverage)

**Why it matters now.** This is dormant-but-spec'd behaviour: `Settings.log_retention_*` configures something the running server does not actually do. A user who sets `LOG_RETENTION_COUNT=50` and then watches `data/logs/` grow forever is correctly confused. **However**, the plan says wiring `prune` belongs in Phase 14 / CCR-016 (doctor + serve preflight). It is therefore not dead — it is **pre-wired for an upcoming ticket**.

**Recommendation: do NOT include in the refactor ticket.** Calling `prune()` from `serve()` is a one-line change but it is exactly what CCR-016 will do. The audit flags this only so the team-lead remembers when CCR-016 picks up.

**Risk / blast radius if wired now.** Low (the function is well-tested in isolation), but it short-circuits ownership of CCR-016. **Punt.**

---

## Suggested ticket breakdown

**One ticket: `CCR-REFACTOR — minor cleanup pass`.**

Findings 1, 2, 3, 4 fit cleanly together: all are dead-code-or-doc-drift removals, all under `src/ccr/claude/manager.py`, `src/ccr/claude/__init__.py`, `src/ccr/bot/keyboards.py`, `src/ccr/bot/handlers/permission.py` (no change — quoted only), `src/ccr/server.py`, and one test file (`tests/test_bot_permission.py`). Ticket scope:

```
## CCR-REFACTOR: minor cleanup of post-CCR-028 dead code and doc drift [todo]

Files:
  - src/ccr/claude/manager.py — drop `claude_session_id` second tuple element
    from `_db_lookup_most_recent_finished` and `_db_lookup_session_by_prefix`
    (both helpers + both call sites + both docstrings); remove unused
    `StaleSessionError` and `StaleToolUseError` classes and `__all__`
    entries.
  - src/ccr/claude/__init__.py — drop the two stale-error re-exports.
  - src/ccr/bot/keyboards.py — drop `"skip"` / `"abort"` from `_LABELS`,
    add `"deny": "Deny"`; refresh the module docstring to reflect that
    CCR-025 has shipped.
  - src/ccr/server.py — fix `asyncio.gather` → `asyncio.wait` in module
    docstring.
  - tests/test_bot_permission.py — rewrite
    `test_keyboard_buttons_match_options_and_callback_data` to use the
    real `["approve", "deny"]` production combo.

Out of scope:
  - Wiring `claude.log.prune` into startup (defer to CCR-016).
  - Removing `McpPermissionRequest.options` (one-line forward seam, leave).
  - Touching anything in `src/ccr/web/` (does not exist yet).
  - Anything that changes wire format, public API, settings, or tests
    outside the one named test.

Acceptance:
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
  - [ ] `mypy src` passes.
  - [ ] `ruff check src tests` passes.
  - [ ] `grep -rn 'StaleSessionError\\|StaleToolUseError\\|_prior_claude_id\\|claude_session_id' src/ tests/` returns no matches.
  - [ ] `grep -n '\"skip\"\\|\"abort\"' src/ccr/bot/keyboards.py` returns no matches.

Notes:
  Mode 1A note for team-lead: skip the architect — additive cleanup, no
  new abstraction, no new wire format, no migration. The reviewer focus
  should be that no production behaviour changes and the keyboard /
  manager cleanup leaves all 385+ existing tests green.
```

This ticket is genuinely small (~20 LOC delta) and the reviewer can verify it by running the existing test suite untouched (except the one rewritten case in `test_bot_permission.py`). The team lead may also reasonably reject it as "not worth the round trip" — the codebase functions correctly with these items left in.

---

## Non-issues considered and rejected

- **README "current state through CCR-006" line.** Stale but README is owned by CCR-017 (the bootstrap ticket) which will rewrite it end-to-end. Leave alone.
- **`cli.py` `doctor` `_stub` handler.** Intentional — Phase 14 / CCR-016 owns the doctor implementation.
- **Settings `log_retention_count` / `log_retention_days` / `web_host` / `web_port` / `cookie_ttl_seconds` / `token_ttl_seconds`.** Defined but unused. All consumed by upcoming CCR-011/012/016 tickets. Leave.
- **`ccr.events.Subscriber` exported but only imported internally.** Reasonable public seam for the upcoming SSE consumer (CCR-012). Leave.
- **`McpPermissionRequest.options` always carries the default list.** One-line forward seam; cost of removal exceeds benefit. Leave.
- **CCR-021 ticket is `[blocked]` not `[closed]`.** Tracking pin per its Review log; matches the spec ("stays open as a tracking pin until CCR-025 lands; revisit if upstream Claude `-p` ever exposes a stdout permission channel"). Architecturally correct; do not move to DONE.
- **CCR-027 (plan-mode) is `[todo]` and depends on CCR-026 which has shipped.** Status is correct (deferred priority; not yet picked up).
- **Multiple `# noqa: ARG001/ARG002` suppressions in handler signatures.** Deliberate — aiogram passes workflow data by name, the unused params are intentional parity for sibling handlers.
- **Many `CCR-NNN:` provenance markers in module docstrings.** These are useful archaeology, not dead code. Leave.
- **`_PendingKeyboard` is a single-underscore "private" name imported across modules.** Mild convention violation but harmlessly load-bearing across `formatting.py` and `server.py`; renaming would touch four files and three tests for no functional gain. Leave.
- **`bot/handlers/ask_user_question.py::_ID8_RE` regex differs from `bot/handlers/session.py::_HEX8_RE`.** Architect plan in `plans/CCR-026-ask-user-question.md` already considered and explicitly rejected lifting these to a shared module — the two regexes guard different identifier spaces. Documented in code comment at `ask_user_question.py:49-56`. Leave.
- **Test file sizes (e.g. `tests/test_session_manager.py` is 2142 lines, `tests/test_bot_passthrough.py` is 994 lines).** Long but not drifting; each test is targeted and well-named. No refactor warranted.
- **Migration consolidation.** Only one migration exists (`0001_initial.py`); nothing to consolidate.
- **`.env.example` vs plan §7.** Compared in detail — the file is in sync with what `Settings` actually loads (including the CCR-025/CCR-026/CCR-029 additions). No drift.
- **Duplicated `AssistantTurn` walking in `_track_subagents` / `_track_ask_user_question` / `_format_assistant_turn`.** Three callers do similar walks but for different reasons (subagent bookkeeping vs. AUQ pairing vs. Telegram rendering). Per the architect plan in CCR-026 (and CLAUDE.md: "three similar lines beats a premature abstraction"), the parallel walks are deliberate. Leave.

## Open questions for team lead

None. The findings are all small, locally-scoped, and verifiable by running the existing test suite. If the team lead decides "not worth a ticket round-trip", that is a defensible call too — none of these block any planned work.
