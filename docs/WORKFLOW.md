# Multi-Agent Workflow

This file is the shared protocol for the agents in `.claude/agents/`. Every agent reads it at the start of a run.

## Roles

The **main session** (the top-level Claude conversation) is the orchestrator and the only entity that dispatches subagents. Subagents in Claude Code cannot invoke other subagents, so all routing — including git operations, branch creation, commits, and PR opening — lives in the main session.

| Agent | Writes code? | Runs code? | Primary outputs |
|---|---|---|---|
| `project-manager` | No | No | Tickets appended to `BACKLOG.md`; stub `docs/<feature>/{BRIEF,CONTEXT}.md` |
| `team-lead` | No | No | Decision: architect-first or skip; dev + reviewer scope briefs; per-ticket `BRIEF.md` + `CONTEXT.md` refresh (Mode 1C, post-developer / pre-reviewer); `BACKLOG.md` / `DONE.md` updates + acceptance ticking + ticket move on completion (Mode 2) |
| `architect` | No (plan file only) | No | Design plan at `plans/CCR-NNN-<slug>.md`; runs only when team-lead asks |
| `python-developer` | Yes | Yes (own tests) | Code + tests under Python backend scope (incl. install/CI/doctor) |
| `web-developer` | Yes | Yes (own tests) | Code + tests under `src/ccr/web/` |
| `reviewer` | No | Yes (test suite at end of pass) | Code review + security findings + test-coverage audit + green-suite confirmation |

There is no sysops agent. Install / CI / doctor work belongs to `python-developer`. There is no QA agent — the reviewer runs the test suite at the end of its pass.

## Flow per ticket

```
main session
  1. picks next [todo] ticket from BACKLOG.md
  2. checks git status; asks user if working tree is dirty
  3. creates branch ccr-NNN-<slug>
  4. marks ticket [in-progress] in BACKLOG.md (entry stays in BACKLOG.md)
  5. dispatches team-lead (Mode 1A — architect-or-dev decision)
       └─→ team-lead returns either:
            (A) DISPATCH: architect CCR-NNN — go to step 6
            (B) dev scope + reviewer focus + DISPATCH: <developer> CCR-NNN — skip to step 8
  6. (only if 5A) dispatches architect
       └─→ architect reads code, writes plans/CCR-NNN-<slug>.md, returns PLAN READY
  7. (only if 5A) dispatches team-lead (Mode 1B — compose dev brief from plan)
       └─→ team-lead returns dev scope + reviewer focus + DISPATCH: <developer> CCR-NNN
  8. dispatches the developer with team-lead's dev scope
       └─→ developer writes code + tests; reports what was done in detail,
           ENDING with a ## BRIEF update note (CCR-NNN) section
  9. dispatches team-lead (Mode 1C — BRIEF/CONTEXT refresh, pre-review)
       └─→ team-lead reads the dev's ## BRIEF update note and "Files created or
           modified" list, refreshes docs/<feature>/BRIEF.md + CONTEXT.md
           IMMEDIATELY (before review), then returns DISPATCH: reviewer CCR-NNN
           with the original ## Reviewer focus block. If the dev's report omits
           the BRIEF update note, team-lead returns DISPATCH back to the
           developer asking for the missing section (no code change), or BLOCKED
           if the gap is structural.
 10. dispatches reviewer (solo — no parallel QA)
       └─→ reviewer does code review + security checks + coverage audit, then
           runs the test suite + lint + types at the end; reports pass/fail.
           Reviewer no longer audits the dev report for a missing BRIEF note —
           team-lead Mode 1C already gated on it. Reviewer MAY still flag
           BRIEF/diff drift as a normal code-review finding.
 11. dispatches team-lead (Mode 2 — verdict)
        - REVIEW FAIL: team-lead returns fix scope → loop to step 8 (fresh dev session;
                       on the next pass Mode 1C will overwrite BRIEF/CONTEXT from the
                       new dev report)
        - REVIEW PASS: team-lead ticks acceptance boxes, flips status to [done],
                       appends the approval Review log line, MOVES the ticket entry
                       from BACKLOG.md to DONE.md → returns APPROVED. BRIEF and
                       CONTEXT were already refreshed in Mode 1C; Mode 2 does not
                       touch them.
 12. main session **stops and waits for user approval** before the final step
 13. on user approval: main session commits, pushes, opens PR (never merges)
```

**Hard rule — user gate before publish.** After team-lead returns `APPROVED`, the main session **must not** stage, commit, push, or run `gh pr create` until the user explicitly approves. End the turn with a short summary (ticket, branch, files changed, acceptance verdict, PR title + body preview) and an explicit ask such as "Ready to commit, push, and open the PR?". Wait for the user's reply. Only after the user says yes (or equivalent) does the main session execute step 12. If the user says no, asks for changes, or stays silent, do not publish.

The developer is dispatched **fresh** each time — including on the fix loop. Pass the team-lead's fix scope verbatim in the new dispatch prompt.

The architect is **optional**. Team-lead Mode 1A decides whether to dispatch it (criteria in `.claude/agents/team-lead.md`). Small, additive tickets skip the architect entirely; new subsystems / first ticket of a feature / load-bearing design decisions go through the architect first.

## Verdict strings (final line of every subagent response)

| Agent | Verdict | Meaning |
|---|---|---|
| project-manager | `TICKETS CREATED: CCR-NNN, ...` | At least one ticket added |
| project-manager | `NO TICKETS CREATED: <reason>` | Decided not to add tickets |
| team-lead (Mode 1A — architect path) | `DISPATCH: architect CCR-NNN` | Main should dispatch the architect with the brief in the response body |
| team-lead (Mode 1A — skip architect, or Mode 1B post-architect) | `DISPATCH: <python-developer\|web-developer> CCR-NNN` | Main should dispatch the named developer with the scope in the response body |
| team-lead (Mode 1C — post-developer, pre-reviewer) | `DISPATCH: reviewer CCR-NNN` | BRIEF/CONTEXT have been refreshed; main should dispatch the reviewer with the focus block reproduced in the response body |
| team-lead (Mode 1C, dev-report incomplete) | `DISPATCH: <python-developer\|web-developer> CCR-NNN` | Dev's report omitted the BRIEF update note; main re-dispatches the dev for an amended report (fix scope in the body) |
| team-lead (fix loop) | `DISPATCH: <python-developer\|web-developer> CCR-NNN` | Same, with the fix scope in the body |
| team-lead (final) | `APPROVED: CCR-NNN` | Ticket complete |
| team-lead (final + last in feature) | `FEATURE COMPLETE: <feature-slug>` | Approved AND BRIEF was the feature's last write |
| team-lead (any) | `BLOCKED: CCR-NNN — <reason>` | Cannot proceed (e.g. acceptance criteria contradict the plan) |
| architect | `PLAN READY: CCR-NNN` | `plans/CCR-NNN-<slug>.md` written; team-lead Mode 1B can compose dev brief from it |
| architect | `BLOCKED: CCR-NNN — <reason>` | Cannot design without resolving a contradiction or scope gap |
| python-developer / web-developer | `READY FOR REVIEW: CCR-NNN` | Implementation done, self-checks green |
| python-developer / web-developer | `BLOCKED: CCR-NNN — <reason>` | Cannot continue |
| reviewer | `REVIEW PASS: CCR-NNN` / `REVIEW FAIL: CCR-NNN — <one-line summary>` | Code-review + security + coverage + green-suite verdict |

The verdict line is parsed by the main session. Anything before it is human-readable detail the main session may quote when dispatching the next agent.

## Ticket files: `BACKLOG.md` and `DONE.md`

Two files at repo root, both append-only:

- **`BACKLOG.md`** holds active tickets in statuses `todo`, `in-progress`, `blocked`. New tickets are appended here by the project-manager.
- **`DONE.md`** holds archived tickets in statuses `done` and `closed`. Entries arrive here by being **moved** from `BACKLOG.md` (verbatim, including the entire `### Review log`) when a ticket reaches a terminal state.

Never delete a ticket. Status changes that stay in BACKLOG.md (e.g. `todo` → `in-progress`, `→ blocked`) only flip the title token. Status changes that hit a terminal state (`→ done`, `→ closed`) move the whole ticket entry, separator and all, from `BACKLOG.md` to `DONE.md`.

### Ticket schema

````markdown
## CCR-<N>: <Title> [<status>]
Phase: <plan phase number>
Feature: <feature-folder-slug>
Files:
  - <path from plan §8 file list>
  - ...
Out of scope:
  - <items from plan>
Acceptance:
  - [ ] <verbatim from plan acceptance criteria>
  - [ ] ...
Depends on: CCR-<N>, CCR-<M>   # omit if none
Notes:
  <PM context, ordering caveats, anything not in the plan>

### Review log
  - <YYYY-MM-DD> <agent>: <one-line note>
````

`<status>` is one of: `todo` `in-progress` `done` `blocked` `closed`.

| Status | File | Meaning |
|---|---|---|
| `todo` | `BACKLOG.md` | Ready to pick up (dependencies met or pending). |
| `in-progress` | `BACKLOG.md` | Picked up; in the architect → dev → review → fix loop. |
| `blocked` | `BACKLOG.md` | Cannot proceed; reason in the Review log. |
| `done` | `DONE.md` | Approved by team-lead. Acceptance criteria verified. |
| `closed` | `DONE.md` | Abandoned, rejected, superseded, or auto-closed without acceptance verification. |

**Status goes inside `[...]` in the title** so it's grep-able as a single line. Search by intent:

```
grep -E '^## CCR-[0-9]+' BACKLOG.md          # active queue
grep -E '^## CCR-[0-9]+' DONE.md             # archive
grep -E '^## CCR-[0-9]+' BACKLOG.md DONE.md  # everything, when looking up by id
```

### Status transitions

- `todo` → `in-progress` — main session, when picking up the ticket. Stays in `BACKLOG.md`.
- `in-progress` → `done` — team-lead, on `APPROVED`. **Moves entry from `BACKLOG.md` to `DONE.md`.**
- `in-progress` → `blocked` — any agent, with a Review log entry explaining what is blocking. Stays in `BACKLOG.md`.
- `blocked` → `todo` / `in-progress` — any agent, when the blocker resolves. Stays in `BACKLOG.md`.
- `todo` / `in-progress` / `blocked` → `closed` — main session, on user direction (rejected, superseded, abandoned). **Moves entry from `BACKLOG.md` to `DONE.md`** with a Review log line summarizing why.
- A ticket stays `[in-progress]` through the entire architect → dev → review → fix loop. There is no `in-review` status.

### Who writes what

- **Main session**: status flip to `[in-progress]` on pickup (in BACKLOG.md); status flip to `[closed]` plus the move from BACKLOG.md → DONE.md when the user closes a ticket; one Review log line per dispatch (`<date> main: dispatched <agent> for <reason>`).
- **Team lead**: status flip to `[done]`; ticking acceptance boxes (`- [x]`); Review log line per verdict; **the move from `BACKLOG.md` to `DONE.md` is part of the team-lead's REVIEW PASS step**.
- **Architect / developers / reviewer**: do not edit `BACKLOG.md` or `DONE.md`. They report in their response body; main and team-lead translate that into log entries and file moves.

### How to move a ticket from BACKLOG.md to DONE.md

When emitting `APPROVED` (team-lead) or `[closed]` (main session):

1. Tick acceptance boxes / append the final Review log line in the BACKLOG.md entry.
2. Cut the entire ticket block — from its `## CCR-NNN: ...` heading through the end of its `### Review log` — including the `---\n` separator that immediately precedes it (or follows the last ticket).
3. Append the cut block to `DONE.md`, preserving the `---\n` separator before it.
4. Verify a single `## CCR-NNN:` line is found across both files (no duplication, no loss).

## `docs/<feature>/`

One folder per feature. Slug names a capability, not a phase (e.g. `core`, `auth`, `chat-bot`, `web-viewer`). PM creates the folder + stubs when generating the first ticket for that feature; multiple tickets across phases share one feature folder.

### `BRIEF.md` (team lead writes / updates in **Mode 1C, every developer pass**)

`BRIEF.md` is the team-lead's primary view of a feature. It is dense, opinionated, and always current — the team-lead must be able to scope an architect or developer brief for a new ticket in this feature **without reading `src/` and without re-reading `CONTEXT.md`**. Created as a stub by PM, refreshed by team-lead in **Mode 1C** (immediately after the developer returns `READY FOR REVIEW`, before the reviewer is dispatched). On a fix loop, Mode 1C overwrites BRIEF/CONTEXT with the new dev report's content; the in-flight stale BRIEF is invisible because BRIEF is only consulted by team-lead Mode 1A when scoping the *next* ticket. Mode 2 does not touch BRIEF.

Format:

```markdown
# Brief: <feature-name>

## Purpose
<1–3 sentences: what the feature delivers, who uses it, why it exists.
Update only when scope changes.>

## Key invariants
- <load-bearing rules — single-session invariant, owner immutability,
  JWT TTL, JSONL append-only with seq derived from line count, etc.
  These are the rules a new ticket must not violate.>
- ...

## Public surface
<Top-level entry points, public functions / classes, bot commands, HTTP routes,
CLI subcommands, with a one-line role each. The team-lead uses this to decide
scope, name the dev's files, and judge the reviewer's coverage. Group by file
or category as helpful.>
- `<symbol or path>` — <role>
- ...

## Subtleties / gotchas
- <Non-obvious behaviour the team-lead must remember when scoping new tickets:
  e.g. "Telegram broadcast pauses while a permission is pending; SSE keeps
  streaming"; "MCP relay is a subprocess, not in-process"; "Owner cannot be
  revoked"; "JWT_SECRET ≥ 32 chars, fail-validation, no fallback".>
- ...

## Cross-feature relations
- depends on: <feature-slug>, ...
- used by: <feature-slug>, ...

## Status
- State: IN PROGRESS | COMPLETE
- Tickets: CCR-NNN, CCR-MMM, ...
- Last updated: CCR-NNN (YYYY-MM-DD)
```

The team-lead refreshes `Public surface`, `Key invariants`, and `Subtleties` whenever a ticket adds, changes, or removes one. The `Status` line always advances: bump `Last updated`, ensure the new ticket id appears in `Tickets:`, and flip `State` to `COMPLETE` when no other ticket for the feature remains in `[todo]` / `[in-progress]` / `[blocked]`. The team-lead derives BRIEF updates from the developer's `## BRIEF update note (CCR-NNN)` section — the team-lead does not read `src/` or `tests/` to confirm. If the dev's report is missing the BRIEF update note, Mode 1C bounces it back as a non-code fix request rather than writing a partial BRIEF.

### `CONTEXT.md` (team lead writes / updates in Mode 1C)

Deeper file-by-file record kept for the architect (and any human reading the repo). Created as a stub by PM, maintained by team lead based on the developer's per-ticket report. Team-lead refreshes CONTEXT.md in **Mode 1C** alongside BRIEF.md (not in Mode 2). The team-lead does not need to re-read CONTEXT.md to scope the *next* ticket — that is what BRIEF.md is for. The architect reads CONTEXT.md (and source) when designing.

Format:

```markdown
# Context: <feature-name>

## Files
- src/ccr/.../foo.py — <one-line role>
- ...

## Relations
- depends on: <feature-slug>, ...
- used by: <feature-slug>, ...

## Change history
- [CCR-NNN]: <short description of what changed in this ticket>
- [CCR-MMM]: ...
```

The change-history entries are append-only. Team lead appends a new entry in Mode 1C (post-developer, pre-reviewer); on a fix loop the next Mode 1C pass overwrites the prior entry's text rather than stacking duplicates.

## Read budgets — who reads what

The team-lead is a manager, not an implementer. The team-lead **does not read `src/` or `tests/`** to scope a ticket and **does not re-read `CONTEXT.md`** as the primary feature view. Instead the team-lead reads:

- The ticket in `BACKLOG.md`.
- The matching phase in `claude-code-remote-plan.md`.
- The feature's `docs/<feature>/BRIEF.md` (always).
- `docs/WORKFLOW.md` and `CLAUDE.md`.
- In Mode 1B only: the architect's `plans/CCR-NNN-<slug>.md` in full.
- In Mode 1C only: the developer's full implementation summary (in particular the `## BRIEF update note` and "Files created or modified" sections).
- In Mode 2 only: the reviewer's full response (the dev report was already consumed in Mode 1C, but is provided again for context if needed).

If `BRIEF.md` does not answer a specific scoping question, the team-lead either asks the question to the user, returns `BLOCKED`, or relies on the architect (in Mode 1A) — the team-lead does **not** open `src/` to find out.

The architect, the developers, the reviewer, and the project-manager continue to read `src/` and `tests/` as needed for their own jobs. The read-budget rule applies only to the team-lead.

## BRIEF update note (subagent → team-lead handoff)

Every subagent that produces a final report includes a `## BRIEF update note (CCR-NNN)` section so the team-lead can refresh `BRIEF.md` without reading the diff or the source. The note is short and structured:

```
## BRIEF update note (CCR-NNN)
- Purpose: <CHANGED | UNCHANGED — if changed, the new 1-sentence purpose, else omit>
- New / changed entries for ## Public surface:
  - <symbol or path> — <one-line role>
  - ...
  (omit the bullet list if nothing changed)
- New / changed Key invariants:
  - <invariant>
  - ...
- New / changed Subtleties / gotchas:
  - <gotcha>
  - ...
- Cross-feature relations to add: depends on <…>; used by <…>
- Status line update: Last updated → CCR-NNN (YYYY-MM-DD); add CCR-NNN to Tickets if missing.
- Feature complete? <YES — flip State to COMPLETE | NO — keep IN PROGRESS>
```

The architect emits the note in its `PLAN READY` response (about what the *plan* changes); the developer emits the note in its `READY FOR REVIEW` response (about what was actually built and may have diverged from the plan). The team-lead then folds the developer's note into `BRIEF.md` verbatim or with light editing in **Mode 1C**, before the reviewer runs — the team-lead is allowed to copy bullets directly without reading the underlying code. The reviewer no longer audits whether the dev wrote the note (Mode 1C is the gate); the reviewer MAY still flag drift between the now-current BRIEF and the actual diff as a code-review finding.

If a subagent has no changes to surface in BRIEF, it still emits the section with `Public surface: no change`, `Key invariants: no change`, etc., to make the absence explicit (otherwise team-lead cannot tell the difference between "nothing changed" and "subagent forgot").

## Phase ordering

The implementation plan (`claude-code-remote-plan.md`) has phase ordering caveats — most importantly **Phase 7 must land before Phase 6** even though Phase 6 is listed first. PM must read the full phase including any `⚠️ ordering note` callout before generating tickets and respect dependencies in the `Depends on:` field.

## How team-lead structures Mode 1A — architect-or-dev decision

The first call for a ticket. Team-lead reads the ticket + plan section + feature CONTEXT and either dispatches the architect or skips straight to a developer brief. Detailed criteria live in `.claude/agents/team-lead.md`. Summary:

**Architect path** (when ticket warrants design work — new subsystem, new abstraction, first ticket of a feature, > ~3 files in scope, plan flags ordering / design subtlety):

```
## Architect brief (CCR-NNN)
<2–6 bullets pointing the architect at what to look at first.>

## Why architect (CCR-NNN)
<1–3 sentences naming the trigger.>

DISPATCH: architect CCR-NNN
```

**Developer-direct path** (small, additive tickets — new chat-bot command, new CLI flag, small bugfix):

```
## Developer scope (CCR-NNN)
<which files to create or modify, what each must do, key signatures from the plan,
which tests to add. Pulled from the ticket's Files: and the plan's code sketches.>

## Reviewer focus (CCR-NNN)
<ticket-specific risks PLUS the literal acceptance commands the reviewer runs at
the end of its pass. Always-on checks (hardcoded secrets, env-var leaks, command
injection, etc.) are implicit; this section names ticket-specific concerns.>

DISPATCH: <python-developer|web-developer> CCR-NNN
```

The main session quotes the relevant section when dispatching each agent.

## How team-lead structures Mode 1B — post-architect dev brief

Triggered when the architect returned `PLAN READY: CCR-NNN`. Team-lead reads `plans/CCR-NNN-<slug>.md` in full and composes the developer brief from it:

```
## Developer scope (CCR-NNN)
<Same shape as Mode 1A's developer-direct path, but composed from the plan file.
Quote the plan's "File layout" / "Public surface" sections verbatim where useful.
Tell the developer to follow plans/CCR-NNN-<slug>.md.>

## Plan reference (CCR-NNN)
- File: plans/CCR-NNN-<slug>.md
- Headline decisions: <2–4 bullets quoting the architect summary>

## Reviewer focus (CCR-NNN)
<Same as Mode 1A. Add a bullet: "Confirm the diff matches the plan; document any
deviation in the dev report.">

DISPATCH: <python-developer|web-developer> CCR-NNN
```

If the architect surfaced "Open questions for team lead" that team-lead cannot resolve from the ticket / plan section alone, team-lead returns `BLOCKED: CCR-NNN — <reason>` so the main session can bring the questions to the user.

## How team-lead structures Mode 1C — post-developer BRIEF/CONTEXT refresh, pre-review

Triggered when the developer returned `READY FOR REVIEW: CCR-NNN`. Team-lead reads the developer's full report (in particular the `## BRIEF update note (CCR-NNN)` section and the "Files created or modified" / "Relations / dependencies" sections), refreshes `docs/<feature>/BRIEF.md` and `docs/<feature>/CONTEXT.md` immediately, and then dispatches the reviewer.

**What Mode 1C edits:**

1. `docs/<feature>/BRIEF.md` from the dev's `## BRIEF update note (CCR-NNN)`:
   - Apply `Public surface` adds/changes/removals.
   - Apply `Key invariants` adds/changes — phrase each as a rule a future ticket might break.
   - Apply `Subtleties / gotchas` adds/changes — non-obvious behaviour to remember when scoping later tickets.
   - Update `Cross-feature relations` if the note added a `depends on:` / `used by:` edge.
   - Update `Status`: bump `Last updated:` to `CCR-NNN (YYYY-MM-DD)`; ensure `Tickets:` includes `CCR-NNN`.
   - Do NOT flip `State: COMPLETE` here — that decision waits for Mode 2 (the ticket isn't approved yet). For now leave `IN PROGRESS`.
2. `docs/<feature>/CONTEXT.md` from the dev's "Files created or modified" / "Relations / dependencies" sections:
   - `## Files`: add or refresh `- <path> — <one-line role>` for files created or substantially changed.
   - `## Relations`: add `depends on:` / `used by:` lines that emerged.
   - `## Change history`: append `- [CCR-NNN]: <short description>` (or update the existing CCR-NNN line on a fix-loop pass — do not stack duplicates for the same ticket).
3. Append a `### Review log` line in `BACKLOG.md`: `<YYYY-MM-DD> team-lead: BRIEF/CONTEXT refreshed, dispatching reviewer`.

**What Mode 1C produces:**

```
## Reviewer focus (CCR-NNN)
<Reproduce the same Reviewer focus block from Mode 1A or 1B verbatim. Reviewer
focus is fixed at scoping time; Mode 1C does not change it. Acceptance commands
to run at the end of the reviewer's pass are reproduced here.>

DISPATCH: reviewer CCR-NNN
```

**If the dev's report omits `## BRIEF update note (CCR-NNN)`:** Mode 1C does NOT write a partial BRIEF. It returns the developer with a small fix scope asking only for the missing report section (no code change, no test re-run):

```
## Fix scope (CCR-NNN)
Your previous READY FOR REVIEW response did not include the
`## BRIEF update note (CCR-NNN)` section required by `docs/WORKFLOW.md`.
Re-emit your full report unchanged plus the missing section, structured per
the format in `docs/WORKFLOW.md §BRIEF update note`. Do NOT re-run any
acceptance commands or change any code — the previous diff is correct, only
the report is incomplete.

DISPATCH: <python-developer|web-developer> CCR-NNN
```

On a fix loop (after `REVIEW FAIL` → developer redo → another `READY FOR REVIEW`), Mode 1C is invoked again and overwrites BRIEF/CONTEXT from the new dev report. Stale BRIEF written from a failed attempt is invisible because team-lead Mode 1A only consults BRIEF when scoping the *next* ticket.

## Final verdict round (team-lead Mode 2, after reviewer report)

When dispatched with reviewer output, team-lead either:

**REVIEW PASS**:
1. In `BACKLOG.md`, tick `- [x]` on each verified `Acceptance:` checkbox (acceptance commands appear in the reviewer's "Test run" section).
2. Append `### Review log` line: `<YYYY-MM-DD> team-lead: approved`.
3. Set ticket status to `[done]`.
4. **Move the ticket entry from `BACKLOG.md` to `DONE.md`** per "How to move a ticket" above. The whole block — title, body, and full Review log — goes verbatim; nothing is dropped.
5. If this was the last ticket for the feature (every other ticket with the same `Feature:` slug across `BACKLOG.md` + `DONE.md` is `[done]` or `[closed]`), flip `State: IN PROGRESS` → `State: COMPLETE` in `docs/<feature>/BRIEF.md`. This is the ONLY BRIEF write Mode 2 performs — content was already refreshed in Mode 1C.
6. Return `APPROVED: CCR-NNN` (or `FEATURE COMPLETE: <feature-slug>` if `State` flipped to COMPLETE).

Mode 2 does NOT write `Public surface`, `Key invariants`, `Subtleties`, `Cross-feature relations`, `Last updated`, `Tickets:`, or `## Change history` — those are Mode 1C's responsibility and must already be correct on disk by the time Mode 2 runs. If Mode 2 detects them stale (e.g. the `Last updated` line still references an older ticket), that is a Mode 1C bug — Mode 2 returns `BLOCKED: CCR-NNN — Mode 1C did not refresh BRIEF/CONTEXT before reviewer dispatch` rather than papering over it.

**REVIEW FAIL**:
1. Append `### Review log` line summarizing the rejection.
2. Leave status as `[in-progress]` (entry stays in `BACKLOG.md`).
3. Write a fix-scope section in the response body — concretely what to fix, citing reviewer findings + failing test output. The developer is fresh and will not see the previous attempt; the fix scope must be self-contained.
4. Return `DISPATCH: <python-developer|web-developer> CCR-NNN`.

(On the next pass through the loop, Mode 1C will overwrite BRIEF/CONTEXT from the corrected dev report.)

## Integration: GitHub PR (never merge automatically)

After team-lead emits `APPROVED: CCR-NNN`, the **main session** prepares the GitHub branch and pull request for the user to merge **manually**. Claude must **never** merge a PR, push to `main`, or run `gh pr merge` on the user's behalf.

### User approval gate (mandatory)

The main session **must pause and wait for explicit user approval** between `APPROVED` and the publish steps below. This is a hard rule — staging, committing, pushing, and `gh pr create` all happen *after* the user says go.

When `team-lead` returns `APPROVED: CCR-NNN`:

1. End the prompt execution. Do not call `git add`, `git commit`, `git push`, or `gh pr create` yet.
2. Post a short summary to the user containing:
   - Ticket id and title.
   - Branch name.
   - Files this ticket touched (one line each).
   - Reviewer verdict (one line) and architect plan path if one was used.
   - The proposed commit message subject.
   - The proposed PR title and a preview of the PR body.
3. End with an explicit ask, e.g. "Ready to commit, push `ccr-NNN-<slug>`, and open the PR?".
4. **Wait.** Do nothing else until the user replies.
5. Proceed to the steps below **only** when the user explicitly approves (e.g. "yes", "go", "ship it", "approved"). On "no", a request for changes, or anything ambiguous, do not publish — answer the user's question or apply the requested change instead, then re-summarize and re-ask.

A user approving once approves only this ticket's publish step. The next ticket's publish step needs its own approval.

### Branch naming

Branch was already created at the start of the ticket as `ccr-NNN-<slug>` where the slug is a 1–4-word kebab-cased summary of the ticket title (e.g. `ccr-005-console-repl`).

### Commit message

One commit per ticket. Format:

```
CCR-NNN: <ticket title>

- <bullet: what was added or changed, mirroring the ticket Files list>
- ...

Acceptance: all <N> criteria verified by reviewer (see DONE.md Review log).
```

Include in the commit: every file the ticket scoped, the ticket's `BACKLOG.md` cut and `DONE.md` paste (the move) plus the ticked acceptance boxes, and any `CONTEXT.md` / `BRIEF.md` updates. Do **not** include unrelated edits. If `pre-commit` or CI fails on push, fix the underlying issue and create a NEW commit — never `--amend` a published commit.

### PR body template

```markdown
## Summary
<1–3 bullets — what this ticket delivers in plain language>

## Files
- <path> — <one-line role>
- ...

## Acceptance
- [x] <verbatim ticket criterion> — verified by reviewer
- [x] ...

## Ticket
DONE.md → CCR-NNN
```

### Steps (main session)

These steps run **only after the user has explicitly approved** the publish (see "User approval gate" above).

1. Stage only the files this ticket owns (plus the ticket's `BACKLOG.md` / `DONE.md` / `CONTEXT.md` / `BRIEF.md` updates — both halves of the BACKLOG → DONE move belong in the same commit): `git add -A` is fine if the working tree has no unrelated noise; otherwise stage by path.
2. Commit using the message format above.
3. Push: `git push -u origin ccr-NNN-<slug>`.
4. Open the PR: `gh pr create --base main --title "CCR-NNN: <title>" --body "<filled template>"`.
5. Report the PR URL back to the user.
6. Switch back to `main` locally only if the user asks; otherwise stay on the feature branch in case follow-up commits are needed.
7. **Stop.** The user reviews and merges the PR manually.

### Hard rules

- Never run `git merge`, `gh pr merge`, `git push origin main`, or `git push --force` against `main`.
- If the PR's CI fails after push, push a follow-up commit on the same branch; do not force-push or amend.
- The user is the only one who decides when a PR lands.
