# Plan: CCR-044 — Persist `[idle]` status; transition to `[running]` on first `claude_session_id`

## Goal

Flip the load-bearing `claude-runtime` invariant that says `SessionStatus.IDLE`
is in-memory only. After this ticket, a fresh `/new` (or plain-text first
prompt) inserts a `Session` row with `status="idle"` and `claude_session_id
IS NULL`; the same fire-and-forget task that today persists `claude_session_id`
on the first `SystemInit` event also flips `status` to `"running"` in a single
atomic UPDATE. `/sessions` and the resume-prefix lookup filter `idle` rows
out at query level so no listing or `/continue <prefix>` ever surfaces a row
that has not yet acquired a Claude id. Imports keep their existing
`status="stopped"` insert. This ticket is the head of the
`claude_session_id`-alignment slice (CCR-043 / 045 / 046 all depend on the
new lifecycle).

## File layout

- `src/ccr/claude/state.py` — modify — rewrite the module docstring: `IDLE`
  is now a real persisted SQL value alongside `running | completed | stopped
  | crashed`. Spell out the lifecycle (INSERT idle → UPDATE running → … →
  finalize completed/stopped/crashed). The `SessionStatus` enum body is
  unchanged (still 5 members).
- `src/ccr/claude/manager.py` — modify
  - `_db_insert_session` (around L1172–1206): write
    `status=SessionStatus.IDLE.value` instead of
    `SessionStatus.RUNNING.value` (current line 1190). Nothing else in the
    method changes.
  - `_consume_events` (around L867–876): no semantic change, still kicks off
    `_update_claude_session_id` on the first `SystemInit` whose
    `event.session_id` is non-empty. The single-shot guard
    `_claude_session_id_persisted` still flips at the same point.
  - `_update_claude_session_id` (around L1063–1088): augment the in-flight
    UPDATE so it ALSO sets `row.status = SessionStatus.RUNNING.value` AND
    publishes a `session.status` `running` event on the bus AFTER the commit
    succeeds. Keep the fire-and-forget shape (try / except → log.exception).
    Add a defensive `WHERE status='idle'` guard at the row level (read row
    via `select`, only flip status if `row.status == SessionStatus.IDLE.value`)
    so a re-delivered `SystemInit` after a process restart between INSERT
    and SystemInit does not reset a row that has already advanced past idle
    (e.g. to `crashed` via `reconcile_orphans`). See "Edge cases" below.
  - `new_session` and `continue_session` (the two `session.status` publishes
    around L391–398 and L521–528): these currently publish `RUNNING` after
    `_db_insert_session`. **Remove** the `RUNNING` publish from `new_session`
    — the running-state publish moves to `_update_claude_session_id` so the
    bus signal coincides with the actual SQL flip. **Keep** the `RUNNING`
    publish in `continue_session` (resume sessions inherit the prior
    conversation; for them the row is inserted as idle but the
    `--resume <id>` argv path already passes a claude_session_id at start
    time, so the row will flip to running on the first SystemInit just as
    new sessions do — but the user-facing "session resumed" reply already
    echoes the new local `Session.id` and the bus running publish should
    fire on SystemInit too). **Decision: move both publishes**. Both call
    sites land the row in `idle`; both transition to `running` only when
    `_update_claude_session_id` fires, and that is where the bus publish
    belongs. Update `self._status` initialization at L359 / L489 to
    `SessionStatus.IDLE` so in-memory and DB agree until SystemInit lands.
    Open question: see the "Open questions" section — this is the only
    decision point where the architect would prefer team-lead confirmation.
  - `_db_finalize_session` (around L1291–1315): the existing
    `if status == SessionStatus.IDLE: raise ValueError` guard is **kept**
    but reworded: the guard now means "finalize must be called with a
    terminal status, never with idle". The guard's purpose is unchanged —
    finalize writes `ended_at` + `exit_reason` and always lands a terminal
    status, never idle.
  - `_db_lookup_resumable_claude_session_id` (around L1243–1289): no code
    change. The existing `WHERE Session.status.in_(_RESUMABLE_STATUSES)`
    where `_RESUMABLE_STATUSES = (COMPLETED, STOPPED)` already excludes idle
    rows naturally because idle ∉ {completed, stopped}. Add a one-line
    docstring sentence noting the new behaviour ("`idle` rows are excluded
    by the `_RESUMABLE_STATUSES` filter, not by the
    `claude_session_id IS NOT NULL` predicate; both conditions hold in
    parallel by construction") so a future change that decouples the two
    filters does not silently regress.
- `src/ccr/claude/import_session.py` — modify (comment-only)
  - The `Session(... status=SessionStatus.STOPPED.value ...)` insert at
    L232–238 is **unchanged**. Add a 2-line comment at the insert site:
    "Imported sessions never pass through `idle` — they arrive with a
    non-NULL `claude_session_id` and a known terminal lifetime, so they go
    straight to `stopped`. `idle` is reserved for fresh `/new` rows whose
    Claude id is still unknown."
- `alembic/versions/0006_add_idle_to_session_status.py` — create (new) —
  doc-only migration. SQLite stores `Session.status` as `String(16)` with
  no CHECK constraint (see `src/ccr/db/models.py` L110), so there is
  nothing to ALTER. `upgrade()` and `downgrade()` are both `pass`. The
  module docstring is the deliverable: it documents that `idle` is now a
  valid persisted value, the new lifecycle, and the explicit
  no-backfill decision (pre-existing rows where `claude_session_id IS NULL`
  keep their existing terminal status — they are legacy-only artefacts).
  See "Alembic migration shape" below for the docstring text.
- `src/ccr/bot/handlers/session.py` — modify
  - `cmd_sessions` (around L267–331): add `Session.status !=
    SessionStatus.IDLE.value` to the listing query
    `select(Session).order_by(...).limit(_SESSIONS_LIMIT)` (L307–310).
    Filter happens at the SQL layer so `_SESSIONS_LIMIT=20` still bounds
    20 _displayed_ rows, not "20 candidates of which some get filtered".
    Update the `cmd_sessions` docstring to add one sentence: "Rows with
    `status='idle'` are filtered out — they have not yet acquired a
    `claude_session_id` and are not addressable via `/continue` / `/rename`."
  - `_format_session_row` (around L334–360): unchanged. The
    `_NULL_CLAUDE_SESSION_ID_MARKER` rendering stays for legacy NULL rows
    that already exist in production DBs (per ticket Notes — supersedes
    CCR-042's marker trajectory but does not remove the constant). Update
    the inline comment at L348–354 to clarify the marker is now a
    legacy-only artefact (after CCR-044, no NEW idle rows reach this
    rendering path because the listing query filters them; only pre-CCR-044
    NULL rows surface). **No change** to the constant or the rendering
    code; comment update only.
  - `cmd_pid` (around L212–227): no code change. The reply text
    `"No active session."` stays identical for the idle path — the user
    is not exposed to the new `idle` lifecycle as a distinct state. The
    branch `if inf.get("status") == SessionStatus.IDLE or ...` continues
    to fire correctly because `info()` returns `SessionStatus.IDLE` when
    `_proc is None` (manager L310–316), which is unchanged. The change
    is that `info()` will ALSO return `SessionStatus.IDLE` when a
    subprocess is held but SystemInit has not yet fired — see the
    in-memory `_status` change in `new_session` / `continue_session`
    above. `cmd_pid` already collapses both into the same reply, which
    is the intended user-facing behaviour. Update the docstring to note
    that "no active session" now covers both the no-subprocess and the
    subprocess-but-pre-SystemInit cases.
  - `cmd_clear` (around L175–209): no code change. The existing
    `prior_status != SessionStatus.IDLE` divider gate (L195) continues to
    work correctly: a `/clear` issued while the manager is fully idle
    (no subprocess) still skips the divider; a `/clear` issued while a
    real session is running (status `RUNNING`) still posts the divider.
    The new edge case — `/clear` issued during the brief window between
    `_db_insert_session` and the first `SystemInit` (in-memory status is
    `IDLE`) — correctly skips the divider, which matches the "idle
    clears stay quiet" invariant (the user has not seen any output yet,
    so a divider would be misleading).
  - `cmd_stop` (around L111–127): no code change. The existing
    `if status == SessionStatus.IDLE` short-circuit (L123) replies "No
    active session." — same as `cmd_pid`. Acceptable user surface; CCR-046
    audits `/stop`'s arg-handling separately.
- `src/ccr/db/models.py` — modify (comment-only) — update the `Session`
  class docstring (L97–99) so the comment "`status` is a string enum
  {`running`, `completed`, `stopped`, `crashed`}" includes `idle` as the
  fifth value, and document the lifecycle (idle on insert → running on
  SystemInit → terminal on finalize / shutdown). No code change.

## Public surface

The only public surface change is observable in `SessionManager.info()`'s
returned `status` value, which can now be `SessionStatus.IDLE` for a
genuinely held but not-yet-initialised session. No new methods, no removed
methods.

```python
# Sketch — illustrative, not the final code.
# src/ccr/claude/manager.py — _update_claude_session_id reworked.

async def _update_claude_session_id(
    self,
    session_id: uuid.UUID,
    claude_session_id: str,
) -> None:
    """Persist Claude's session id AND flip status idle -> running.

    Single-UPDATE atomic: the same row write sets ``claude_session_id``
    and advances ``status`` from ``idle`` to ``running``. Defensive
    in-Python guard: if the row's status has already advanced past idle
    (e.g. orphan-reconciled to ``crashed`` after a process restart
    between INSERT and the first SystemInit), only ``claude_session_id``
    is set; status is left alone. Failures are logged and swallowed —
    losing this write is non-fatal (``/continue`` will skip the NULL row
    on the no-prefix path; user can re-issue ``/new``).
    """
    try:
        async with self._db_factory() as db:
            row = await db.scalar(select(Session).where(Session.id == session_id))
            if row is None:
                return
            row.claude_session_id = claude_session_id
            if row.status == SessionStatus.IDLE.value:
                row.status = SessionStatus.RUNNING.value
                # publish AFTER commit so subscribers see a consistent
                # SQL state. Done outside the `async with` block.
                publish_running = True
            else:
                publish_running = False
            await db.commit()
    except Exception:
        log.exception(
            "session_manager.claude_session_id_update_failed",
            session_id=str(session_id),
            claude_session_id=claude_session_id,
        )
        return
    if publish_running:
        await self._bus.publish(
            "session.status",
            {
                "session_id": session_id,
                "status": SessionStatus.RUNNING,
                "ts": datetime.now(UTC),
            },
        )
        # Update in-memory status to mirror the DB. Only flip if the
        # session is still the live one — a teardown could have raced.
        if self._session_id == session_id and self._status == SessionStatus.IDLE:
            self._status = SessionStatus.RUNNING
```

```python
# Sketch — illustrative, not the final code.
# src/ccr/claude/manager.py — _db_insert_session: status flipped to IDLE.

row = Session(
    id=session_id,
    started_at=started_at,
    status=SessionStatus.IDLE.value,   # was: RUNNING.value
    started_by_tg_user_id=started_by_tg_user_id,
    first_prompt=truncated_prompt,
)
```

```python
# Sketch — illustrative, not the final code.
# src/ccr/bot/handlers/session.py — cmd_sessions query filters idle rows.

rows = (
    await db.scalars(
        select(Session)
        .where(Session.status != SessionStatus.IDLE.value)
        .order_by(Session.started_at.desc())
        .limit(_SESSIONS_LIMIT),
    )
).all()
```

## Patterns and prior art

- **Reuse**: `src/ccr/claude/manager.py:1063` (existing
  `_update_claude_session_id` shape — fire-and-forget, `try/except → log
  exception`, fresh DB session per write). The plan extends this method
  in place rather than introducing a sibling task. Single UPDATE in one
  transaction is the natural extension; SQLAlchemy commits both column
  writes together because both happen on the same loaded `row` before
  `db.commit()`. No risk of half-write.
- **Reuse**: `src/ccr/claude/manager.py:248–250, 362, 492` —
  `_claude_session_id_persisted` single-shot guard. Already correct for
  blocking duplicate writes; the new in-row `WHERE status='idle'` guard is
  defence-in-depth for the orthogonal "row advanced past idle while we
  weren't looking" case. The two guards together cover both
  same-process-double-SystemInit (single-shot) and cross-restart
  re-delivery (row-state guard).
- **Reuse**: `_RESUMABLE_STATUSES = (COMPLETED, STOPPED)` filter in
  `_db_lookup_resumable_claude_session_id` (manager L212–215). This already
  excludes idle and running rows. The architect confirms no change is
  needed; the docstring update spells this out so the next reader sees the
  intent.
- **Reuse**: `alembic/versions/0004_add_sessions_name.py` /
  `0005_drop_sessions_claude_session_id_unique.py` filename + revision
  pattern (`<NNNN>_<short_name>.py`, `revision: str =
  "<NNNN>_<short_name>"`, explicit `down_revision`). The new migration
  follows the same shape with `0006_add_idle_to_session_status` and
  `down_revision = "0005_drop_sessions_claude_session_id_unique"`.
- **Avoid**: introducing a CHECK constraint on `Session.status`. SQLite
  CHECK constraints require `batch_alter_table` to recreate the table;
  the upside (DB-level enforcement) is small for a value-only enum where
  the application is the only writer. The plan keeps the SQL column as
  `String(16)` and lets the application be the source of truth — same
  precedent as the existing `String(16)` declaration with no constraint.
- **Avoid**: a separate `_status_flipped` flag. `_claude_session_id_persisted`
  is already gated on the same SystemInit single-shot path; piggy-backing
  the status flip onto the same UPDATE means the two writes succeed or
  fail together. Adding a sibling guard would create a second
  partial-success state (claude_session_id written but status not
  flipped, or vice versa) that today's code does not have.

## Abstractions

No new abstraction. The change is a value-level invariant flip
(`status="idle"` is now a write target) plus a one-call extension to an
existing fire-and-forget method. Three concrete edits (`_db_insert_session`,
`_update_claude_session_id`, `cmd_sessions` query) is exactly the
"three-similar-lines is better than premature abstraction" CLAUDE.md
guidance allows.

## Dependencies

- Depends on:
  - `src/ccr/claude/state.py::SessionStatus` (existing enum, no change to
    members).
  - `src/ccr/db/models.py::Session.status` (existing `String(16)` column).
  - `EventBus` topic `session.status` (existing; the running publish moves
    from `new_session`/`continue_session` to `_update_claude_session_id`).
  - The single-shot `_claude_session_id_persisted` guard (CCR-036
    invariant, preserved verbatim).
- Used by:
  - **CCR-043** (`/rename current`) — uses `info()`'s `status="idle"`
    return to surface the distinct "active session has no claude_session_id
    yet" error path. After this ticket, `manager.info()` returns
    `{"status": SessionStatus.IDLE, "session_id": <UUID>, "pid": <int>,
    "started_at": <datetime>}` for the brief window between
    `_db_insert_session` and the first SystemInit. CCR-043 can detect this
    via `inf["status"] == SessionStatus.IDLE and inf["session_id"] is not
    None` and emit the "no `claude_session_id` yet" reply.
  - **CCR-045** — drops the local-UUID prefix from `cmd_new` /
    `cmd_continue` / `cmd_pid` replies. The pid-based phrasing is a
    consequence of `claude_session_id` being NULL during idle.
  - **CCR-046** — audits `/stop` against the new lifecycle. After this
    ticket, `/stop` works on idle sessions because `cmd_stop` already
    short-circuits on `status == IDLE` with "No active session." which is
    the user-facing surface CCR-046 needs to confirm.

## Edge cases the developer must handle

- **Process restart between INSERT (idle) and SystemInit.** The bot crashes
  after `_db_insert_session(status="idle")` writes but before
  `_update_claude_session_id` fires. On restart, `reconcile_orphans` (manager
  L544–577) runs and looks for rows where `status == RUNNING.value` — it
  will NOT see the idle row (idle ≠ running), so the row stays as
  `(idle, claude_session_id=NULL)` indefinitely. **Decision**: this is
  acceptable. The row is invisible to `/sessions` (filtered) and to
  `/continue` (no claude_session_id). Pre-existing operator behaviour for
  cleanup is unchanged; a follow-up ticket can extend `reconcile_orphans`
  to also reconcile `idle` → `crashed` on restart, but that is **out of
  scope** here. **However**, the developer must extend `reconcile_orphans`
  to ALSO sweep `status == IDLE.value` rows into `crashed` because
  otherwise an idle row left over from a crash is permanently stuck.
  See "Open questions" below — this is the one design point where the
  architect would prefer the team-lead to confirm.
- **Re-delivered SystemInit after restart.** If somehow a SystemInit
  arrives for a session whose row is no longer idle (e.g. orphan-reconciled
  to `crashed`), the `WHERE row.status == IDLE` guard in
  `_update_claude_session_id` skips the status flip and only writes
  `claude_session_id`. The single-shot `_claude_session_id_persisted` flag
  is process-local, so it does not block the second-process write — the
  defensive in-row guard is what catches that case.
- **Concurrent SystemInit.** The `_claude_session_id_persisted` flag is
  set BEFORE the task is created (manager L872–876), so two synchronous
  observations of SystemInit in the same `_consume_events` loop
  iteration cannot both fire the task. `asyncio.create_task` returns
  immediately; the task body is the only writer. Stream-json has at most
  one SystemInit per session in observed runs; this guard is precautionary.
- **`new_session` with prompt = None vs. with prompt.** `new_session`
  awaits `self._init_event` (via `_init_event.wait()`) only when a prompt
  is non-empty (manager L400–421). The `_init_event` is set in
  `_consume_events` upon SystemInit (L869). The new flow does not break
  this: the prompt-arrival waits for SystemInit, which is the same
  trigger that fires the new `_update_claude_session_id`. The two are
  ordered by `_consume_events`: `init_event.set()` happens first
  (synchronous), then `create_task` for the DB write (also synchronous on
  the loop). The `await proc.send_user_turn(prompt)` then runs while the
  DB task is in flight — both correctly proceed without serializing.
  Acceptable; the in-memory `_status` flip in
  `_update_claude_session_id` happens *after* the DB commit, so the
  prompt may technically be sent while `self._status == IDLE`. This is
  the existing behaviour (running was set in-memory before the DB write
  too); the bus event order is what matters and the bus
  `session.status` publish is the user-visible signal.
- **Failure of `_update_claude_session_id`.** The fire-and-forget wraps
  in `try/except → log.exception`. If the DB commit fails, the row stays
  `(idle, claude_session_id=NULL)`. Same recovery path as the previous
  edge case: the row is invisible to `/sessions` and `/continue`. The
  user-facing impact is that `/sessions` will not list the running
  session until the DB recovers. Acceptable; pre-existing
  `last_event_at_update_failed` has the same posture.
- **`cmd_pid` reply during idle window.** The user observes
  `"No active session."` even though a Claude subprocess is technically
  running. This is intentional: from the user's perspective, the session
  is not yet usable (Claude has not finished init); presenting it as
  "running" would be misleading. CCR-043's `/rename current` is the
  command that distinguishes the two states for the rename use case. No
  other command needs to distinguish.
- **`reconcile_orphans` extension.** This call (manager L544–577)
  currently only flips `status='running' → 'crashed'`. After this ticket,
  rows can also be stuck at `status='idle'` after a crash. The developer
  MUST extend `reconcile_orphans` to also sweep
  `status == SessionStatus.IDLE.value` rows into `crashed` (with
  `exit_reason="bot restart while initialising"` or similar) to keep the
  DB self-cleaning. Test coverage in
  `tests/test_session_manager.py::test_reconcile_orphans_*` (existing)
  must extend to cover this case.

## Test surface

- `tests/test_session_manager.py::test_new_session_inserts_idle_status` —
  `new_session(prompt=None, started_by_tg_user_id=...)` lands a `Session`
  row with `status == SessionStatus.IDLE.value` and
  `claude_session_id IS NULL` BEFORE the fake claude emits SystemInit.
  Verify by selecting the row immediately after the `new_session` call
  returns AND BEFORE pumping the fake claude's events. Use the
  fake-claude fixture pattern from existing
  `test_system_init_persists_claude_session_id` (manager test L898).
- `tests/test_session_manager.py::test_system_init_flips_idle_to_running_atomically` —
  Existing `test_system_init_persists_claude_session_id` extended (or new
  test) — after the fake claude emits SystemInit, the row is
  `status == "running"` AND `claude_session_id == "<observed-id>"`.
  Single SELECT round-trip should observe both writes (proves single
  UPDATE, no torn-write window). The single-UPDATE assertion is implicit:
  if both columns reflect the new state in one round-trip and the test
  uses an in-memory SQLite (no concurrent writers), there is no torn-write
  exposure in practice; the developer should pin behaviour rather than
  the SQL shape.
- `tests/test_session_manager.py::test_second_system_init_is_noop` — Emit
  TWO SystemInit events from the fake claude (one with `session_id="abc"`,
  another with `session_id="xyz"`). Assert: row's
  `claude_session_id == "abc"` (single-shot guard fires only once). Status
  is `"running"` after the first event and unchanged after the second.
- `tests/test_session_manager.py::test_update_claude_session_id_skips_status_flip_on_non_idle_row` —
  Manually pre-mutate the row to `status="crashed"` (simulating an orphan
  reconcile between INSERT and SystemInit). Trigger the SystemInit emit.
  Assert: `claude_session_id` is written (the column update fires), but
  status stays `"crashed"` (the row-state guard fires). This pins the
  defensive `WHERE status='idle'` clause so a future code change cannot
  silently regress it.
- `tests/test_session_manager.py::test_db_lookup_resumable_skips_idle_rows` —
  Insert one `idle` row, one `completed` row, one `stopped` row. Call
  `_db_lookup_resumable_claude_session_id(session_id_prefix=None)` →
  returns the most-recent of (completed, stopped) by `started_at`,
  never the idle row. Variant: pass a prefix that matches only the
  idle row's claude_session_id (force-set it for the test). Expect
  `SessionNotFoundError` because idle rows are filtered out by the
  `_RESUMABLE_STATUSES` predicate. (This is the
  "filter holds even if claude_session_id is somehow non-NULL" defensive
  case — confirms the architect's assumption that the existing filter is
  sufficient transitionally.)
- `tests/test_session_manager.py::test_reconcile_orphans_sweeps_idle_rows` —
  Pre-seed an `idle` row. Call `manager.reconcile_orphans()`. Assert: the
  row is now `status == "crashed"` with an appropriate `exit_reason`,
  `ended_at` set. Variant for `running` rows is already covered.
- `tests/test_session_manager.py::test_session_status_idle_blocks_db_finalize_call` —
  The existing `_db_finalize_session` IDLE guard (manager L1299) is
  preserved; add a regression test calling `_db_finalize_session` with
  `status=SessionStatus.IDLE` raises `ValueError`. (Defensive — the
  manager never calls finalize with idle, but the guard catches future
  refactors.)
- `tests/test_bot_session_handlers.py::test_sessions_filters_idle_rows` —
  Pre-seed three rows: one `idle` (with NULL claude_session_id), one
  `running` (with non-NULL claude_session_id), one `completed` (with
  non-NULL claude_session_id). `/sessions` lists the running and
  completed rows; the idle row is absent from the reply.
- `tests/test_bot_session_handlers.py::test_sessions_still_renders_legacy_null_marker_for_non_idle_rows` —
  Pre-seed a row with `status="completed"` and
  `claude_session_id=None` (legacy artefact). `/sessions` lists it with
  the `--------` marker. Confirms the marker rendering is preserved for
  legacy rows even though no NEW idle/null rows surface.
- `tests/test_bot_session.py` — existing `cmd_pid` test on the idle
  branch (FakeManager `status=IDLE`) still asserts the
  `"No active session."` reply. No new assertion needed; if the test
  drifts, the existing assertion catches it.
- `tests/test_bot_session.py` — existing `cmd_clear` divider test must
  continue to pass with no change. Verify the divider is NOT broadcast
  when `prior_status == IDLE`, including the new "subprocess held but
  pre-SystemInit" idle.
- Migration round-trip: `pytest tests/test_db_models.py` (existing) plus
  any test that runs `alembic upgrade head` should already cover the
  no-op migration. The dev should not add a new migration test; the
  doc-only migration is exercised by the existing alembic boot path.

## Out of scope

- Removing the `_NULL_CLAUDE_SESSION_ID_MARKER` constant or its
  `<code>--------</code>` rendering from `cmd_sessions` — kept for
  legacy NULL rows. Removal is a separate follow-up (see the ticket
  Notes) gated on confidence that no NULL rows remain in production DBs.
- Backfilling pre-existing NULL-`claude_session_id` rows. Pre-CCR-044
  legacy rows keep their existing terminal status (`stopped` /
  `completed` / `crashed`) and remain non-addressable via `/continue` /
  `/rename` exactly as today.
- Chat-output cleanup of local UUIDs in `cmd_new` / `cmd_continue` /
  `cmd_pid` reply strings (handled by CCR-045).
- `/rename current` semantics (handled by CCR-043 rework).
- `/stop` argument rekeying (handled by CCR-046).
- Adding a SQL CHECK constraint on `Session.status` or otherwise
  enforcing the enum at the DB layer — sticking with application-level
  enforcement (consistent with prior precedent).
- Web viewer / SSE filtering of idle rows — `/api/sessions` is unrelated
  to this ticket and lives under web-viewer scope.

## Open questions for team lead

1. **Should `reconcile_orphans` extend to sweep `idle` rows on restart?**
   The architect's recommendation is **YES**: extend it in this ticket so
   the DB self-cleans. The cost is one additional SELECT branch (3 lines
   of code) and one new test. The alternative — leaving idle rows stuck
   indefinitely — is acceptable but creates the surprising state where
   a row exists in the DB but is invisible to every command. **Default**:
   include the extension; if the team-lead wants to defer it to a
   follow-up, it is small enough to ship separately.

2. **Should the `session.status: running` bus publish move from
   `new_session`/`continue_session` to `_update_claude_session_id`?**
   The architect's recommendation is **YES**: the bus signal should
   coincide with the actual `idle → running` SQL transition; otherwise
   SSE / web-viewer subscribers see a `running` status before the row
   has a `claude_session_id`. The cost is one method call moved (~5
   lines) and a bus-event-order regression test. The alternative —
   keeping the publish at `new_session` time — would mean SSE clients
   see `running` while the DB row is still `idle`, which is a small but
   real inconsistency. **Default**: move the publish.

If the team-lead prefers either default flipped, the plan adapts trivially
— both decisions are local edits to `_update_claude_session_id` and
`new_session`/`continue_session`.

---

## BRIEF update note (CCR-044)

- Purpose: UNCHANGED (still owns subprocess + event infra + lifecycle FSM).
- New / changed entries for ## Public surface:
  - `SessionStatus.IDLE` (existing enum member) is now a real persisted
    SQL value; `Session.status` accepts `idle | running | completed |
    stopped | crashed` (was: `running | completed | stopped | crashed`).
  - `SessionManager.info()` may return `{"status": SessionStatus.IDLE,
    "session_id": <UUID>, "pid": <int>, "started_at": <datetime>}` for
    the window between `_db_insert_session` and the first SystemInit
    event — distinct from the no-subprocess case where every field is
    `None`.
  - `SessionManager.reconcile_orphans()` — extended (if Open question 1
    is approved): now also sweeps `status='idle'` rows into `crashed`
    on restart, alongside the existing `status='running'` sweep.
- New / changed Key invariants:
  - **Replace** the prior invariant
    "`SessionStatus.IDLE` is intentionally absent from `Session.status`
    SQL writes — the SQL column accepts `running | completed | stopped
    | crashed` only" with:
    "**`Session.status` accepts five values: `idle | running | completed
    | stopped | crashed`. A row is INSERTed with `status='idle'` and
    `claude_session_id IS NULL` on every fresh `/new`; the same
    fire-and-forget task that persists `claude_session_id` from the
    first `SystemInit` event also flips `status` to `'running'` in a
    single atomic UPDATE. Imports (`import_claude_session`) bypass
    `idle` and insert directly with `status='stopped'`. Terminal states
    (`completed`, `stopped`, `crashed`) are written by
    `_db_finalize_session` and `reconcile_orphans`. The
    `_db_finalize_session` IDLE guard remains: finalize is never called
    with `status=idle`."**
  - **Add**: "`_db_lookup_resumable_claude_session_id` excludes idle
    rows by the existing `_RESUMABLE_STATUSES = (COMPLETED, STOPPED)`
    filter; the `claude_session_id IS NOT NULL` skip is a parallel
    guarantee (idle rows have NULL claude_session_id by construction)."
  - **Strengthen**: "`_claude_session_id_persisted` is the in-process
    single-shot guard for the SystemInit-driven UPDATE; the UPDATE
    itself carries an in-row `WHERE status='idle'` defensive guard
    (read row, skip status flip if already past idle) so a re-delivered
    SystemInit after a process restart does not regress the row's
    status."
- New / changed Subtleties / gotchas:
  - "`/sessions`, `cmd_pid`, and `cmd_clear` treat the brief
    pre-SystemInit window the same as the no-subprocess case: the user
    sees `"No active session."` for `/pid` and `/sessions` filters the
    row out. This is intentional — Claude is not yet usable. CCR-043's
    `/rename current` is the only command that distinguishes the two
    states, via `inf['session_id'] is not None and inf['status'] ==
    IDLE`."
  - "The `session.status: running` bus publish moved from `new_session` /
    `continue_session` to `_update_claude_session_id`, so SSE / web-viewer
    subscribers observe the running signal coincident with the SQL
    flip — not before."
- Cross-feature relations: no change (still depends on core, used by
  chat-bot + web-viewer).
- Status line update: Last updated → CCR-044 (YYYY-MM-DD on landing); add
  CCR-044 to Tickets if missing (ticket already listed).
- Feature complete? NO — the slice continues with CCR-043, 045, 046.
