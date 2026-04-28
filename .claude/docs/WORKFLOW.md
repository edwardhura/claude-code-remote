# Multi-Agent Workflow

This file is the shared protocol for the agents in `.claude/agents/`. Every agent reads it at the start of a run.

## Roles

The **main session** (the top-level Claude conversation) is the orchestrator and the only entity that dispatches subagents. Subagents in Claude Code cannot invoke other subagents, so all routing — including git operations, branch creation, commits, and PR opening — lives in the main session.

| Agent | Writes code? | Runs code? | Primary outputs |
|---|---|---|---|
| `project-manager` | No | No | Tickets in `TICKETS.md`; stub `.claude/docs/<feature>/{BRIEF,CONTEXT}.md` |
| `team-lead` | No | No | Scope briefs for dev/QA/reviewer; `BRIEF.md`, `CONTEXT.md`, `TICKETS.md` updates on completion |
| `python-developer` | Yes | Yes (own tests) | Code + tests under Python backend scope (incl. install/CI/doctor) |
| `web-developer` | Yes | Yes (own tests) | Code + tests under `src/ccr/web/` |
| `qa` | No | Yes | Test execution report + missing-test gaps |
| `reviewer` | No | No | Security review (secrets, vulnerabilities, env-var leaks) |

There is no sysops agent. Install / CI / doctor work belongs to `python-developer`.

## Flow per ticket

```
main session
  1. picks next [todo] ticket
  2. checks git status; asks user if working tree is dirty
  3. creates branch ccr-NNN-<slug>
  4. marks ticket [in-progress] in TICKETS.md
  5. dispatches team-lead (initial scope)
       └─→ team-lead returns: dev scope + QA plan + reviewer focus
  6. dispatches the developer with team-lead's dev scope
       └─→ developer writes code + tests; reports what was done in detail
  7. dispatches qa + reviewer in parallel (one message, two Agent calls)
       └─→ qa runs ticket acceptance + relevant tests; reports pass/fail
       └─→ reviewer scans diff for secrets / vulnerabilities; reports pass/fail
  8. dispatches team-lead (verdict)
        - if either failed: team-lead returns fix scope → loop to step 6 (fresh dev session)
        - if both passed: team-lead updates TICKETS.md, CONTEXT.md, BRIEF.md (if last ticket)
                          → returns APPROVED
  9. main session **stops and waits for user approval** before the final step
 10. on user approval: main session commits, pushes, opens PR (never merges)
```

**Hard rule — user gate before publish.** After team-lead returns `APPROVED`, the main session **must not** stage, commit, push, or run `gh pr create` until the user explicitly approves. End the turn with a short summary (ticket, branch, files changed, acceptance verdict, PR title + body preview) and an explicit ask such as "Ready to commit, push, and open the PR?". Wait for the user's reply. Only after the user says yes (or equivalent) does the main session execute step 10. If the user says no, asks for changes, or stays silent, do not publish.

The developer is dispatched **fresh** each time — including on the fix loop. Pass the team-lead's fix scope verbatim in the new dispatch prompt.

## Verdict strings (final line of every subagent response)

| Agent | Verdict | Meaning |
|---|---|---|
| project-manager | `TICKETS CREATED: CCR-NNN, ...` | At least one ticket added |
| project-manager | `NO TICKETS CREATED: <reason>` | Decided not to add tickets |
| team-lead (initial) | `DISPATCH: <agent> CCR-NNN` | Main should dispatch the named developer with the scope in the response body |
| team-lead (fix loop) | `DISPATCH: <agent> CCR-NNN` | Same, with the fix scope in the body |
| team-lead (final) | `APPROVED: CCR-NNN` | Ticket complete |
| team-lead (final + last in feature) | `FEATURE COMPLETE: <feature-slug>` | Approved AND BRIEF written |
| team-lead (any) | `BLOCKED: CCR-NNN — <reason>` | Cannot proceed (e.g. acceptance criteria contradict the plan) |
| python-developer / web-developer | `READY FOR REVIEW: CCR-NNN` | Implementation done, self-checks green |
| python-developer / web-developer | `BLOCKED: CCR-NNN — <reason>` | Cannot continue |
| qa | `QA PASS: CCR-NNN` / `QA FAIL: CCR-NNN — <one-line summary>` | Test verdict |
| reviewer | `REVIEW PASS: CCR-NNN` / `REVIEW FAIL: CCR-NNN — <one-line summary>` | Security verdict |

The verdict line is parsed by the main session. Anything before it is human-readable detail the main session may quote when dispatching the next agent.

## TICKETS.md

Single file at repo root. Append-only — never delete tickets, only update status in the title.

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

`<status>` is one of: `todo` `in-progress` `done` `blocked`.

**Status goes inside `[...]` in the title** so it's grep-able as a single line:
```
grep -E '^## CCR-[0-9]+' TICKETS.md
```

### Status transitions

- `todo` → `in-progress` — main session, when picking up the ticket
- `in-progress` → `done` — team-lead, on `APPROVED`
- `in-progress` → `blocked` — any agent, with a Review log entry explaining what is blocking
- A ticket stays `[in-progress]` through the entire dev → QA → review → fix loop. There is no `in-review` status.

### Who writes what to TICKETS.md

- **Main session**: status flip on pickup; Review log line per dispatch (`<date> main: dispatched <agent> for <reason>`).
- **Team lead**: status flip to `[done]`; ticking acceptance boxes (`- [x]`); Review log line per verdict.
- **Developers / QA / reviewer**: do not edit `TICKETS.md`. They report in their response body; main and team-lead translate that into log entries.

## `.claude/docs/<feature>/`

One folder per feature. Slug names a capability, not a phase (e.g. `core`, `auth`, `chat-bot`, `web-viewer`). PM creates the folder + stubs when generating the first ticket for that feature; multiple tickets across phases share one feature folder.

### `BRIEF.md` (team lead writes / updates)

Created as a stub by PM, filled in by team lead when the feature's last ticket is approved. Format:

```markdown
# Brief: <feature-name>

## Overview
<one short paragraph: what the feature delivers and why it matters>

## Files
- <path> — <one-line role>
- ...

Status: COMPLETE | IN PROGRESS
Tickets: CCR-NNN, CCR-MMM
```

### `CONTEXT.md` (team lead writes / updates)

Created as a stub by PM, maintained by team lead based on the developer's per-ticket report. Format:

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

The change-history entries are append-only. Team lead appends a new entry when emitting `APPROVED`.

## Phase ordering

The implementation plan (`claude-code-remote-plan.md`) has phase ordering caveats — most importantly **Phase 7 must land before Phase 6** even though Phase 6 is listed first. PM must read the full phase including any `⚠️ ordering note` callout before generating tickets and respect dependencies in the `Depends on:` field.

## How team-lead structures the initial scope brief

When dispatched without a verdict-needed signal (i.e., as the first call for a ticket), team-lead reads the ticket + plan section + feature CONTEXT and produces a single response with three sections, then ends with `DISPATCH: <agent> CCR-NNN`:

```
## Developer scope (CCR-NNN)
<which files to create or modify, what each must do, key signatures from the plan,
which tests to add. Pulled from the ticket's Files: and the plan's code sketches.>

## QA test plan (CCR-NNN)
<which acceptance commands to run literally, which test files exercise the code,
specific scenarios to verify, coverage expectations.>

## Reviewer focus (CCR-NNN)
<what kinds of risk to scan for given what the ticket touches: e.g. "this ticket
introduces JWT minting — check no secret is logged, no hardcoded fallback secret,
PyJWT call uses HS256 not none". Always-on checks (hardcoded secrets, env-var
leaks, command injection in subprocess calls) are implicit; this section names
ticket-specific concerns.>

DISPATCH: <python-developer|web-developer> CCR-NNN
```

The main session quotes the relevant section when dispatching each agent.

## Final verdict round (team-lead, after QA + reviewer report)

When dispatched with QA + reviewer outputs, team-lead either:

**Both pass**:
1. Tick `- [x]` on each verified `Acceptance:` checkbox.
2. Append `### Review log` line: `<YYYY-MM-DD> team-lead: approved`.
3. Set ticket status to `[done]`.
4. Update `.claude/docs/<feature>/CONTEXT.md` based on the developer's report:
   - Add/update `## Files` entries for files created or substantially changed.
   - Add `## Relations` entries (`depends on:` / `used by:`) that emerged.
   - Append `- [CCR-NNN]: <short description>` to `## Change history`.
5. If this was the last ticket for the feature, update `BRIEF.md` (Overview, Files, Status: COMPLETE).
6. Return `APPROVED: CCR-NNN` (or `FEATURE COMPLETE: <feature-slug>` if BRIEF was written).

**Either fails**:
1. Append `### Review log` line summarizing the rejection (one line per failing agent).
2. Leave status as `[in-progress]`.
3. Write a fix-scope section in the response body — concretely what to fix, citing QA / reviewer output. The developer is fresh and will not see the previous attempt; the fix scope must be self-contained.
4. Return `DISPATCH: <agent> CCR-NNN`.

## Integration: GitHub PR (never merge automatically)

After team-lead emits `APPROVED: CCR-NNN`, the **main session** prepares the GitHub branch and pull request for the user to merge **manually**. Claude must **never** merge a PR, push to `main`, or run `gh pr merge` on the user's behalf.

### Branch naming

Branch was already created at the start of the ticket as `ccr-NNN-<slug>` where the slug is a 1–4-word kebab-cased summary of the ticket title (e.g. `ccr-005-console-repl`).

### Commit message

One commit per ticket. Format:

```
CCR-NNN: <ticket title>

- <bullet: what was added or changed, mirroring the ticket Files list>
- ...

Acceptance: all <N> criteria verified by qa + reviewer (see TICKETS.md Review log).
```

Include in the commit: every file the ticket scoped, the ticket's `TICKETS.md` status / Review-log update, and any `CONTEXT.md` / `BRIEF.md` updates. Do **not** include unrelated edits. If `pre-commit` or CI fails on push, fix the underlying issue and create a NEW commit — never `--amend` a published commit.

### PR body template

```markdown
## Summary
<1–3 bullets — what this ticket delivers in plain language>

## Files
- <path> — <one-line role>
- ...

## Acceptance
- [x] <verbatim ticket criterion> — verified by qa
- [x] ...

## Ticket
TICKETS.md → CCR-NNN
```

### Steps (main session)

1. Stage only the files this ticket owns (plus the ticket's `TICKETS.md` / `CONTEXT.md` / `BRIEF.md` updates): `git add -A` is fine if the working tree has no unrelated noise; otherwise stage by path.
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
