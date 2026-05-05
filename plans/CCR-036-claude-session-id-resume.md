# Plan: CCR-036 — `claude_session_id` column, resume rework, `session save` CLI, sync skill

## Goal
Restructure resume semantics around Claude's own `session_id` — the value Claude emits in its `system.init` event and stores under `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`. Add a nullable `sessions.claude_session_id` column with a partial unique index, capture the id from `system.init` as it streams, switch `/continue` to `claude --resume <claude_session_id>`, and add a `session save <id>` dual-entrypoint command (CLI + console REPL) that imports an existing local Claude session into our DB without copying its JSONL. Ship a `templates/ccr/skills/sync-claude-session-with-remote/SKILL.md` wrapper so a paired user can register their local session with one prompt to Claude.

After this ticket lands, `/continue` resumes the exact Claude conversation previously captured (not "the most recent finished row in our DB"), sessions started outside the bot can be brought into the bot's surface via `session save`, and the column CCR-037 needs for its standardised `/sessions` listing already exists.

## File layout
- `alembic/versions/0003_add_sessions_claude_session_id.py` — create — new migration: add nullable `claude_session_id TEXT` column to `sessions`, plus partial unique index `ix_sessions_claude_session_id_not_null` on `claude_session_id` where `claude_session_id IS NOT NULL`.
- `src/ccr/db/models.py` — modify — add `claude_session_id: Mapped[str | None]` field on `Session`; declare the partial unique index in `__table_args__` with the same `sqlite_where` pattern that `paired_users.is_owner` uses today.
- `src/ccr/claude/import_session.py` — create — module home of the shared async import function. Mirrors `ccr.auth.pairing` shape: pure functions taking `AsyncSession` first, no global state, errors raised as a focused exception class. Owns encoded-cwd derivation and JSONL parsing.
- `src/ccr/claude/manager.py` — modify — capture `claude_session_id` from `SystemInit` and persist it inline (fire-and-forget DB update, mirroring `_update_last_event_at`); switch `continue_session` to look up `claude_session_id` instead of our UUID; pass that string to `ClaudeProcess(resume=<id>)` so the subprocess gets `--resume <claude_session_id>`. Drop `_db_lookup_most_recent_finished` and `_db_lookup_session_by_prefix` (replace, not extend — the prior-row UUID lookup is what's being removed). Tighten `NoPriorSessionError` / `SessionNotFoundError` semantics so a row with `claude_session_id IS NULL` raises `NoPriorSessionError` ("no resumable session"), preserving the bot-side reply contract.
- `src/ccr/claude/process.py` — modify — argv builder: when `resume` is a non-empty string, emit `--resume <value>`; when `resume is True`, keep emitting `--continue` (the no-arg call site still wants this). Comment the slot so the CCR-029 ordering invariant stays loud. (No new public API — `start(*, resume: bool | str = False, mcp_argv=...)` already accepts `str`; today only `True` is used.)
- `src/ccr/cli.py` — modify — add a `session` subcommand group (parallel to `pair`); register `session save <claude_session_id>` as the first member via a new `_add_session_subcommands(...)` helper and `_cmd_session_save` handler. Reuse `_with_session` and exit-code conventions from the `pair` family. Update the top-level subparsers `metavar` to include `session`.
- `src/ccr/console/app.py` — modify — register `session save` in the REPL: extend `_STATIC_COMMAND_WORDS` with `session` and `save`; add `"session save": _cmd_session_save` to `COMMANDS`; bump `_match_command`'s prefix sweep to `(2, 1)` (already there — confirm 2-token coverage); add `_cmd_session_save` handler shaped like `_cmd_pair_approve`; extend `_cmd_help` output.
- `templates/ccr/skills/sync-claude-session-with-remote/SKILL.md` — create — Claude Code skill markdown that documents the wrapper flow (detect most-recent local Claude session OR accept an explicit id; invoke `python -m ccr session save <id>`; surface the row prefix and `/sessions` hint). Plain in-tree commit; `templates/` is not yet a submodule.
- `tests/test_session_save_cli.py` — create — end-to-end coverage of the dual-entrypoint flow: hand-crafted Claude JSONL at a tmp location, owner seeded, exercise both `python -m ccr session save <id>` (subprocess via `cli.main`) and `python -m ccr console --once "session save <id>"` against the same DB; assert the resulting row's `claude_session_id`, `started_at`, `started_by_tg_user_id`, `status="stopped"`; assert double-import is rejected.
- `tests/test_console.py` — modify — extend with `session save` REPL coverage parallel to `pair approve`: missing-arg usage hint, success path, duplicate-id handling, missing-file error path.
- `tests/test_session_manager.py` — modify — add `claude_session_id` extraction on `SystemInit` (fake-claude scripts already emit `system.init` with `session_id`); add resume-by-claude-id path; remove or rewrite the four existing tests that walked the `_db_lookup_*` paths (most-recent / by-prefix / unknown-prefix / excludes-crashed). New shape: seed a row with `claude_session_id="abc-123"` and `status=COMPLETED`, call `continue_session()`, assert argv contains `--resume abc-123`; seed a row with `claude_session_id IS NULL`, assert `NoPriorSessionError`.
- `tests/test_claude_process.py` — modify — argv test pinning that `resume="abc-123"` emits `--resume abc-123` in the right slot (between fixed control flags and the permission block); keep the existing `resume=True` → `--continue` test green.
- `tests/test_db_models.py` — modify — round-trip a `Session` row with `claude_session_id` set and unset; insert two rows with the same non-NULL `claude_session_id` and assert `IntegrityError`; insert two rows with `claude_session_id IS NULL` and assert no error (partial unique index).

## Public surface

### `src/ccr/claude/import_session.py` (new module)

```python
# Sketch — illustrative, not the final code.
"""Import an existing local Claude session into the CCR DB.

Pure async helpers, no global state. The CLI (`ccr session save`) and the
REPL (`ccr console` → `session save`) both call :func:`import_claude_session`
through their own thin wrappers, mirroring the dual-entrypoint pattern that
`ccr.auth.pairing.approve` already serves.
"""

from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class ImportSessionError(Exception):
    """Base class for `import_claude_session` failures."""


class ClaudeSessionFileNotFoundError(ImportSessionError):
    """Raised when no `~/.claude/projects/*/<id>.jsonl` matches the id."""


class DuplicateClaudeSessionError(ImportSessionError):
    """Raised when a row with this `claude_session_id` already exists."""


class NoOwnerError(ImportSessionError):
    """Raised when no owner exists in `paired_users` (cannot seed `started_by_tg_user_id`)."""


def encoded_cwd_for(path: Path) -> str:
    """Encode an absolute filesystem path into Claude's project-dir naming.

    Format observed in `~/.claude/projects/`: replace every '/' with '-'
    (so `/Users/edward/foo` → `-Users-edward-foo`). The leading '/' becomes
    a leading '-'. Used both for forward lookup (encode our cwd) and for
    decoding back during scans.
    """


def find_claude_session_file(
    claude_session_id: str,
    *,
    projects_root: Path | None = None,
) -> Path:
    """Return the JSONL path for `claude_session_id`, scanning every project dir.

    Default `projects_root` is `~/.claude/projects/`. The function does NOT
    require the caller to know which `cwd` Claude used — it walks every
    `<projects_root>/*/` directory and returns the first match for
    `<claude_session_id>.jsonl`. Raises `ClaudeSessionFileNotFoundError`
    on miss. The id-as-filename convention was confirmed in the Step-0
    probe (see "Encoded cwd / file layout — observed format" below).
    """


async def import_claude_session(
    db: AsyncSession,
    claude_session_id: str,
    *,
    projects_root: Path | None = None,
) -> "Session":
    """Read Claude's local JSONL for `claude_session_id` and write a row.

    Steps:
      1. `find_claude_session_file(...)` — raise on miss.
      2. Stream the JSONL once, capturing:
           - the first line that carries a top-level `timestamp` (skip
             `file-history-snapshot` lines whose `timestamp` is nested) →
             `started_at`.
           - first-user-prompt content (NOT used for `name` in this ticket;
             see "CCR-037 coordination" below).
      3. Look up the owner via `pairing.get_owner(db)` — raise `NoOwnerError`
         if `None`.
      4. Insert a `Session(claude_session_id=..., started_at=...,
         started_by_tg_user_id=owner.tg_user_id, status="stopped",
         id=uuid.uuid4())`. Commit.
      5. On `IntegrityError` from the partial unique index → roll back, raise
         `DuplicateClaudeSessionError(claude_session_id)`.

    Returns the persisted `Session`.

    Does NOT copy the JSONL into `data/logs/` — Claude owns its history.
    Our `data/logs/<our_uuid>.jsonl` is only created when a continuation
    actually streams (`SessionManager.continue_session` does that).
    """
```

### `src/ccr/claude/manager.py`

```python
# Sketch — illustrative, not the final code.

# In _consume_events, on SystemInit: persist claude_session_id inline.
# Mirrors the `_schedule_last_event_at_update` fire-and-forget pattern,
# but single-shot per session (no debounce — runs once on first SystemInit).
if isinstance(event, SystemInit):
    self._init_event.set()
    self._skills = list(event.skills)
    if event.session_id and not self._claude_session_id_persisted:
        self._claude_session_id_persisted = True
        asyncio.create_task(
            self._update_claude_session_id(session_id, event.session_id),
            name=f"claude-session-id-{session_id}",
        )

# Replaces _db_lookup_most_recent_finished + _db_lookup_session_by_prefix.
async def _db_lookup_resumable_claude_session_id(
    self,
    *,
    session_id_prefix: str | None,
) -> str | None:
    """Return the `claude_session_id` of a resumable row, or None.

    No prefix → the most recent finished row whose `claude_session_id IS
    NOT NULL` (excludes crashed and excludes legacy NULL rows).
    With prefix → the same set, filtered to rows whose `id.hex[:8] ==
    prefix` (in-Python, same rationale as today's lookup).
    """
```

`continue_session` raises:
- `SessionAlreadyRunningError` — unchanged.
- `NoPriorSessionError` — no resumable row OR resumable row has `claude_session_id IS NULL` (legacy). Bot-side reply unchanged.
- `SessionNotFoundError` — given prefix but no resumable row matches.
- `SessionError` — claude binary missing / subprocess fails to start. Unchanged.

### `src/ccr/claude/process.py`

No new symbol. Argv block updates to:

```python
# Sketch — illustrative, not the final code.
if self._resume is True:
    argv.append("--continue")
elif isinstance(self._resume, str) and self._resume:
    argv.extend(["--resume", self._resume])
# (existing comment: ordering invariant — keep this between fixed flags
# and permission flags.)
```

### `src/ccr/cli.py`

```python
# Sketch — illustrative, not the final code.

def _cmd_session_save(args: argparse.Namespace) -> None:
    rc = asyncio.run(_async_session_save(args.claude_session_id))
    if rc != 0:
        raise SystemExit(rc)


async def _async_session_save(claude_session_id: str) -> int:
    from ccr.claude.import_session import (
        ClaudeSessionFileNotFoundError,
        DuplicateClaudeSessionError,
        ImportSessionError,
        NoOwnerError,
        import_claude_session,
    )
    exit_code = 0

    async def _run(session: AsyncSession) -> None:
        nonlocal exit_code
        try:
            row = await import_claude_session(session, claude_session_id)
        except ClaudeSessionFileNotFoundError as exc:
            sys.stderr.write(f"{exc}\n")
            exit_code = 1
            return
        except DuplicateClaudeSessionError as exc:
            sys.stderr.write(f"Already imported: {exc}\n")
            exit_code = 1
            return
        except NoOwnerError:
            sys.stderr.write("No owner registered. Pair the owner first.\n")
            exit_code = 1
            return
        except ImportSessionError as exc:
            sys.stderr.write(f"{exc}\n")
            exit_code = 1
            return
        sys.stdout.write(f"Imported Claude session {claude_session_id} as {row.id.hex[:8]}\n")

    await _with_session(_run)
    return exit_code


def _add_session_subcommands(session_subparsers):
    save_parser = session_subparsers.add_parser(
        "save",
        help="Import an existing local Claude session.",
    )
    save_parser.add_argument(
        "claude_session_id",
        help="Claude session id (UUID Claude reports in its system.init event).",
    )
    save_parser.set_defaults(func=_cmd_session_save)
```

The `metavar` for the top-level subparsers becomes `"{serve,console,pair,session,doctor,init-db}"`.

### `src/ccr/console/app.py`

```python
# Sketch — illustrative, not the final code.

_STATIC_COMMAND_WORDS = (
    "pair", "list", "pending", "approve", "revoke", "invite",
    "session", "save",
    "status", "help", "exit", "quit",
)

async def _cmd_session_save(args: list[str], settings: Settings) -> None:
    if not args:
        _emit("Usage: session save <claude_session_id>")
        return
    claude_session_id = args[0]

    async def _run(session: AsyncSession) -> None:
        from ccr.claude.import_session import (
            ClaudeSessionFileNotFoundError,
            DuplicateClaudeSessionError,
            ImportSessionError,
            NoOwnerError,
            import_claude_session,
        )
        try:
            row = await import_claude_session(session, claude_session_id)
        except ClaudeSessionFileNotFoundError as exc:
            _emit(str(exc))
            return
        except DuplicateClaudeSessionError as exc:
            _emit(f"Already imported: {exc}")
            return
        except NoOwnerError:
            _emit("No owner registered. Pair the owner first.")
            return
        except ImportSessionError as exc:
            _emit(str(exc))
            return
        _emit(f"Imported Claude session {claude_session_id} as {row.id.hex[:8]}")

    await _with_session(settings, _run)


COMMANDS["session save"] = _cmd_session_save
# `_match_command` already sweeps prefix lengths (2, 1) — no change needed.
```

`_cmd_help` adds:

```
  session save <claude_session_id>
                            Import an existing local Claude session.
```

### `alembic/versions/0003_add_sessions_claude_session_id.py`

```python
# Sketch — illustrative, not the final code.
revision: str = "0003_add_sessions_claude_session_id"
down_revision: str | None = "0002_add_paired_users_timezone"

def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(sa.Column("claude_session_id", sa.Text(), nullable=True))
    op.create_index(
        "ix_sessions_claude_session_id_not_null",
        "sessions",
        ["claude_session_id"],
        unique=True,
        sqlite_where=sa.text("claude_session_id IS NOT NULL"),
    )

def downgrade() -> None:
    op.drop_index("ix_sessions_claude_session_id_not_null", table_name="sessions")
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("claude_session_id")
```

## Patterns and prior art

- **Dual-entrypoint shared function.** `src/ccr/auth/pairing.py::approve` is the worked example. `src/ccr/cli.py:169-186` (`_async_pair_approve`) and `src/ccr/console/app.py:121-136` (`_cmd_pair_approve`) both call it; output strings are pinned. Mirror this verbatim: thin REPL/CLI wrappers around `ccr.claude.import_session.import_claude_session`.
- **Partial unique index.** `paired_users.is_owner` index in `src/ccr/db/models.py:58-65` and `alembic/versions/0001_initial.py:39-45` shows the `sqlite_where` shape. Reuse exactly.
- **Subcommand group pattern.** `_add_pair_subcommands` at `src/ccr/cli.py:281-323` — one `add_parser(...)` per leaf, `set_defaults(func=...)`, separate top-level subparser for the group. `_add_session_subcommands` is a one-for-one copy with one leaf today.
- **Console 2-token prefix sweep.** `_match_command` at `src/ccr/console/app.py:267` already iterates `(2, 1)` — `"session save"` slots in without changing the sweep.
- **Mid-session DB writes.** `_update_last_event_at` (`src/ccr/claude/manager.py:999-1012`) is the template for the inline `claude_session_id` update: open a fresh session via `self._db_factory()`, scalar-load the row, mutate, commit, exception-log on failure. Single-shot per session (a `_claude_session_id_persisted: bool` flag mirrors `_saw_result_success`).
- **Migration shape.** `alembic/versions/0002_add_paired_users_timezone.py` is the 1:1 template — `batch_alter_table` + `add_column` for the column, plus `op.create_index(..., sqlite_where=...)` for the partial unique index (taken from `0001_initial.py:39-45`). Reversible: `drop_index` + `drop_column`.
- **Avoid:** writing the import logic on `SessionManager`. The manager already owns lifecycle for a *running* subprocess; importing a *finished* foreign session is a separate concern that doesn't touch the asyncio lock, the bus, or the JSONL log. A free function under `ccr.claude.import_session` keeps `SessionManager` focused.

## Abstractions

**One new module, no new abstraction.** `ccr.claude.import_session` is a concrete sibling to `ccr.auth.pairing` — same posture: free async functions, focused exception class, `AsyncSession` first, no global state. There is exactly one concrete caller pair (CLI + console) for one operation (`session save`); that is the same call-count justification as `ccr.auth.pairing.approve`. The module is named for its scope, not for an abstraction it represents — adding `session list` later would also live here without churn.

`SessionManager` keeps its existing structure. The only addition is a new private helper `_update_claude_session_id(session_id, claude_session_id)` that mirrors `_update_last_event_at` and a `_claude_session_id_persisted: bool` flag that gates the one-shot write. No new class, no factored-out "DB writer" abstraction — three call sites is below the bar.

## Dependencies

- **Depends on:**
  - `src/ccr/claude/events.py::SystemInit.session_id` (exists since CCR-007; already typed `str | None`).
  - `src/ccr/auth/pairing.py::get_owner` for the import function's owner lookup.
  - `src/ccr/db/models.py::Session` (modified here).
  - `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl` filesystem layout (Step-0 probe — see below).
  - The `claude` CLI accepting `--resume <session_id>` for non-interactive runs (confirmed via `claude --help` output: `-r, --resume [value]`).
- **Used by:**
  - CCR-037 — relies on `claude_session_id` for the `/sessions` listing column.
  - Bot `/continue` (`src/ccr/bot/handlers/session.py:108-150`) — unchanged at the call site, but its underlying behaviour now resumes via Claude's id rather than ours.
  - The new `templates/ccr/skills/sync-claude-session-with-remote/SKILL.md` skill — invokes `python -m ccr session save` from inside Claude Code.

## Encoded cwd / file layout — observed format (Step-0 probe)

Probed against `~/.claude/projects/` on this machine (claude v2.1.123, dozens of session files across multiple cwds):

- **Directory naming:** `<absolute-path>` with every `/` replaced by `-`. The leading `/` becomes a leading `-`. Examples:
  - `/Users/edward/workspace/claude-code-remote` → `-Users-edward-workspace-claude-code-remote`.
  - `/tmp/ccr-021-probe` → `-tmp-ccr-021-probe` (verified — appears as `-private-tmp-ccr-021-probe-cwd` because macOS resolves `/tmp` → `/private/tmp` before encoding).
  - **Caveat:** macOS path canonicalisation can resolve symlinks (notably `/tmp` → `/private/tmp`) before encoding. Do NOT rely on round-tripping `os.getcwd()` against the encoding — instead, **scan all `~/.claude/projects/*/` dirs** for a file named `<claude_session_id>.jsonl`. The id is globally unique across project dirs (UUID4); a scan is correct and avoids the symlink-resolution trap.
- **File naming:** `<sessionId>.jsonl`, where `<sessionId>` is the same id Claude reports in `system.init.session_id` on its `-p --output-format=stream-json` stream. Each line in the file carries `sessionId == <id>` (verified across multiple files).
- **First-event timestamp:** the very first line is sometimes a `type:"file-history-snapshot"` whose `timestamp` is nested under `snapshot.timestamp` rather than top-level. Use the **first line that carries a top-level `timestamp`** for `started_at`. The first `type:"user"` line always has one.
- **First user prompt:** the first `type:"user"` event with `isMeta != true` and `message.content` not starting with `<command-` (slash commands and local-command captives prefix their content). Used for CCR-037 auto-derive — out of scope here (see "CCR-037 coordination").
- **Sub-directory artefacts:** some session ids have a sibling directory (e.g. `<id>/subagents/`) — ignore. Only the `<id>.jsonl` file is what we read.
- **Resume command:** `claude --resume <claude_session_id>` (option present, accepts a session id; verified via `claude --help`).

The plan settles option (b) of the architect brief's design call 3: scan all `~/.claude/projects/*/` for `<id>.jsonl` rather than encoding our cwd. No further probe needed.

## Edge cases the developer must handle

- **`claude_session_id` capture timing — chosen: persist immediately on `SystemInit`, fire-and-forget.** Modelled on `_update_last_event_at`. The team-lead's design call 2 chose this over batched-with-next-write because (a) the `system.init` event is emitted exactly once per session at the very start, so the cost is one extra DB write per session lifetime — negligible; (b) a process crash *after* `system.init` but before the next batched write would lose the id forever, breaking `/continue` for that whole session; (c) `_update_last_event_at` already proves the fire-and-forget pattern is fine. The write is gated by a `_claude_session_id_persisted: bool` flag so a second `system.init` (theoretical, never observed) is a no-op. Failures are logged via `log.exception` and do NOT raise — same as `_update_last_event_at`. Note: the BRIEF's "Log-before-publish" invariant is unchanged; this DB write happens *after* `log.append` and `bus.publish`, in a detached task. Add a new BRIEF subtlety bullet capturing this.
- **`continue_session` against a row with `claude_session_id IS NULL`** (legacy or pre-CCR-036): raise `NoPriorSessionError("No prior session to continue.")` so the bot's existing handler at `src/ccr/bot/handlers/session.py:138-140` keeps showing the same canned message. Verified: that handler does `await msg.answer(str(exc))` — nothing to update bot-side.
- **`continue_session` with prefix matching a row whose `claude_session_id IS NULL`:** raise `NoPriorSessionError` (same as above), NOT `SessionNotFoundError`. Rationale: the row exists, but it isn't resumable. Avoid leaking "found but unresumable" through `SessionNotFoundError` whose message is "No session found with id <prefix>" — the user would be told to look harder when there's nothing more to find.
- **Double-import of the same `claude_session_id`:** the partial unique index raises `IntegrityError` on commit. The import function catches it (or pre-checks via `select(Session).where(Session.claude_session_id == ...)` — developer's call; either is acceptable, the index is the authoritative guard) and raises `DuplicateClaudeSessionError(claude_session_id)`. The CLI/REPL wrappers map it to `"Already imported: <id>"`.
- **No owner registered when `session save` runs:** raise `NoOwnerError` from the import function; the wrappers print `"No owner registered. Pair the owner first."` and exit non-zero (CLI) / continue REPL (console). This matches the existing rule that `started_by_tg_user_id` is non-null on the row's interesting paths; we don't have a Telegram user context for an offline imported session.
- **`session save` against a session id Claude has never seen:** `find_claude_session_file` returns no match → `ClaudeSessionFileNotFoundError(f"No local Claude session found with id {claude_session_id}")`. Wrappers print verbatim and exit 1.
- **`session save` against a session file with no parseable `timestamp`:** read the whole file, fall back to `datetime.now(UTC)` for `started_at` and log a `import_session.no_timestamp` warning. Do not abort the import — the row is still useful for `/continue`. Document this in the function's docstring.
- **Empty / malformed JSONL lines:** silently skip (`json.JSONDecodeError`) — same drift-tolerance posture as `parse_event`. Document in the function.
- **`status="stopped"` (not "completed"):** the import function writes `stopped` because we cannot prove the foreign session ran to a successful `result` event. `stopped` is in `_RESUMABLE_STATUSES` — `/continue` will pick the row up.
- **MCP relay vs. resume:** `--resume` and the existing `--mcp-config` flags are mutually compatible (Claude treats `--mcp-config` as session-scope, applied after restoration). The slot order in `process.py` keeps `--resume` before the permission block — same as today's `--continue` slot. No new tests beyond the argv pin in `test_claude_process.py`.
- **`sessions.first_prompt`:** existing column — already populated by `new_session(prompt=...)`. The import function does NOT write `first_prompt` (the foreign session's first user prompt may be a slash command or a multi-block content, and the column is documented as "the prompt that started OUR session"). Leave NULL. CCR-037's `name` column is the proper home for the human-readable seed.
- **`templates/` is currently a regular directory, not a submodule.** Confirmed: `.gitmodules` does not exist; `templates/` does not exist. Create `templates/ccr/skills/sync-claude-session-with-remote/SKILL.md` as a plain in-tree commit. The ticket flags this risk; the architect's verdict is: no submodule coordination is required for this ticket. If CCR-017 lands first and converts `templates/` to a submodule, this ticket's PR will need to be rebased — but at the time of dispatch CCR-017 is `[todo]`, not `[in-progress]`, so this is highly unlikely.
- **Removing `_db_lookup_most_recent_finished` and `_db_lookup_session_by_prefix`.** These are private (`_`-prefixed). One test (`test_session_manager.py:546`) calls `_db_lookup_most_recent_finished` directly via `# noqa: SLF001` — that test must be rewritten or deleted. Audit all `_db_lookup_*` references in tests and replace the call sites with the new `_db_lookup_resumable_claude_session_id` (or, better, drive the test through `continue_session` end-to-end).

## CCR-037 coordination — chosen: leave `name` to CCR-037

Architect design call 4 settles to **(c)** of the brief: this ticket does **not** add the `sessions.name` column or seed it. The import function writes `name=None` implicitly (the column will not exist yet). CCR-037's migration adds `name` and its `manager.py` rule auto-fills it from the first user prompt of running sessions; backfilling imported rows is out of scope for both tickets per CCR-037's `Out of scope` list ("Backfilling `name` for legacy rows — leave NULL").

Why not pull CCR-037's migration forward into CCR-036:
- CCR-037's auto-fill rule lives in `manager.py` and the listing render lives in `bot/handlers/session.py`. Pulling the column forward into CCR-036 without those handlers means the column sits NULL on every new session until CCR-037 lands — the migration is more useful when it lands together with the auto-fill that gives it values.
- CCR-037 explicitly `Depends on: CCR-036` and is already designed for the column-lands-with-the-rule shape.
- The PM's "PM recommends sequencing this ticket BEFORE CCR-037" steer is already satisfied by CCR-036's `claude_session_id` column landing first; the `name` column is a CCR-037 concern.

The import function's docstring should note: "First user prompt is intentionally not written to any column in this ticket; CCR-037 handles `name` for going-forward sessions and explicitly leaves imported rows NULL."

## Test surface

- `tests/test_session_save_cli.py::test_session_save_cli_imports_local_jsonl` — write a hand-crafted `~/.claude/projects/-tmp-fake-cwd/<id>.jsonl` under `tmp_path` (override `projects_root` via env or via a `_make_settings`-style fixture passing `projects_root=tmp_path/.claude/projects`), seed an owner, run `python -m ccr session save <id>` via `cli.main([...])`, assert the row exists with the right fields.
- `tests/test_session_save_cli.py::test_session_save_console_once_imports_local_jsonl` — same setup, run `cli.main(["console", "--once", f"session save {id}"])`, assert the row.
- `tests/test_session_save_cli.py::test_session_save_double_import_rejected` — run twice, assert second exits non-zero with `"Already imported"` in the output and only one row exists.
- `tests/test_session_save_cli.py::test_session_save_unknown_id_errors` — no JSONL on disk, assert exit 1 with file-not-found message.
- `tests/test_session_save_cli.py::test_session_save_no_owner_errors` — JSONL exists, no owner seeded, assert exit 1 with the no-owner message.
- `tests/test_session_save_cli.py::test_session_save_uses_first_top_level_timestamp` — JSONL with a leading `file-history-snapshot` (no top-level timestamp) followed by a `user` event with one; assert `row.started_at` is parsed from the user event's `timestamp`.
- `tests/test_session_save_cli.py::test_session_save_does_not_copy_jsonl` — run, assert `data/logs/<row.id>.jsonl` does NOT exist after the import.
- `tests/test_session_save_cli.py::test_encoded_cwd_for_round_trips_simple_path` — quick unit test of `encoded_cwd_for(Path("/Users/edward/foo"))` → `"-Users-edward-foo"`.
- `tests/test_console.py::test_session_save_missing_arg` — `once="session save"` → "Usage: session save <claude_session_id>".
- `tests/test_console.py::test_session_save_via_repl_imports_row` — JSONL on disk, owner seeded, `once="session save <id>"` → success message.
- `tests/test_console.py::test_session_save_completer_includes_session_save` — `build_completer` returns a completer whose word list includes `session` and `save`.
- `tests/test_console.py::test_help_lists_session_save` — `_cmd_help` output contains `session save <claude_session_id>`.
- `tests/test_session_manager.py::test_system_init_persists_claude_session_id` — drive fake-claude through `{"type":"system","subtype":"init","session_id":"abc-123"}`, await the row, assert `row.claude_session_id == "abc-123"`. Replace existing tests that drove the old `_db_lookup_most_recent_finished` / by-prefix paths.
- `tests/test_session_manager.py::test_continue_session_uses_claude_session_id_resume` — seed a row with `status=COMPLETED`, `claude_session_id="abc-123"`, call `continue_session(...)`, assert argv (via `FAKE_CLAUDE_ARGV_FILE`) contains `--resume`, `abc-123` adjacently.
- `tests/test_session_manager.py::test_continue_session_skips_rows_with_null_claude_session_id` — seed a finished row with `claude_session_id=None`, assert `NoPriorSessionError` (no-arg path); seed alongside a resumable one and assert the resumable wins.
- `tests/test_session_manager.py::test_continue_session_by_prefix_against_null_row_raises_no_prior` — seed a finished row with `claude_session_id=None` and call `continue_session(session_id_prefix=row.id.hex[:8])`, assert `NoPriorSessionError`.
- `tests/test_claude_process.py::test_argv_resume_string_emits_resume_flag` — instantiate `ClaudeProcess(settings, resume="abc-123")`, run with `FAKE_CLAUDE_ARGV_FILE`, assert `["--resume", "abc-123"]` appear in order in the argv slot between fixed control flags and the permission block.
- `tests/test_claude_process.py::test_argv_resume_true_still_emits_continue` — regression: keep the `resume=True` → `--continue` test green.
- `tests/test_db_models.py::test_session_round_trips_claude_session_id` — round-trip a row with the column set; with NULL.
- `tests/test_db_models.py::test_session_partial_unique_index_on_claude_session_id` — insert two rows with the same non-NULL value, assert `IntegrityError`; insert two rows with NULL, assert no error.
- `tests/test_db_models.py::test_alembic_round_trips_claude_session_id_migration` — `alembic upgrade head && alembic downgrade base && alembic upgrade head` against a tmp DB; the column appears, disappears, reappears.

## Out of scope

- `session list`, `session rename` — leave the CLI group + REPL dispatcher open for them but do not implement now.
- Copying Claude's JSONL into `data/logs/` on import.
- Web-viewer surfaces of `claude_session_id`.
- Migrating existing `sessions` rows to backfill `claude_session_id`.
- The `/sessions` listing format that uses `claude_session_id` — that's CCR-037.
- `sessions.name` column and auto-derive — CCR-037.
- Bot-side `/continue` reply changes — the canned messages are unchanged; only the underlying mechanism changed.
- Dropping the `--continue` argv path: still used internally? No — but `resume=True` is still typed-supported in `ClaudeProcess` and the test pinning it stays. We deliberately avoid changing the public API of `ClaudeProcess.start`.
- Updating `templates/` to a submodule — that's CCR-017's job.

## Open questions for team lead

None. All four design calls are settled in this plan:

1. **Where the import logic lives:** `src/ccr/claude/import_session.py` (new module under `claude` subpackage). Justified above.
2. **`claude_session_id` capture timing:** persist immediately on `SystemInit`, fire-and-forget (mirroring `_update_last_event_at`). Justified in "Edge cases" above.
3. **Encoded-cwd discovery:** scan all `~/.claude/projects/*/` for `<id>.jsonl` rather than encoding our cwd, because macOS path canonicalisation breaks the round-trip. Step-0 probe done in this session — encoded format and file naming documented above.
4. **`name` seeding coordination with CCR-037:** leave `name` to CCR-037; this ticket only adds `claude_session_id`. Justified in "CCR-037 coordination" above.
