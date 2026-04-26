# Multi-Agent Workflow

This file is the shared protocol for the agents in `.claude/agents/`. Every agent reads it at the start of a run.

## Roles

Routing is owned by the **main session** (the top-level Claude conversation), not a subagent — Claude Code subagents cannot invoke other subagents. The agents below are the only invokable subagents:

| Agent | Writes code? | Primary outputs |
|---|---|---|
| `project-manager` | No | Tickets in `TICKETS.md`; stub `.claude/docs/<feature>/{BRIEF,CONTEXT}.md` |
| `team-lead` | No | Ticket status updates in `TICKETS.md`; `BRIEF.md` on completion |
| `python-developer` | Yes | Code under `src/ccr/{bot,auth,claude,console,db,events}/`, tests, `CONTEXT.md` updates |
| `web-developer` | Yes | Code under `src/ccr/web/` (FastAPI, viewer frontend), tests, `CONTEXT.md` updates |
| `sysops` | Yes | `install.sh`, `.github/workflows/`, `doctor` subcommand, `.env.example`, `CONTEXT.md` updates |

## Handoffs (main session routes everything)

Subagents do **not** invoke each other. When a subagent finishes, it returns a structured one-line verdict back to the main session. The main session parses it and dispatches the next agent. Routing rules live in `CLAUDE.md` § "Routing rules (main session)".

Required final-line formats:

- Project manager: `TICKETS CREATED: CCR-NNN, CCR-MMM, ...` or `NO TICKETS CREATED: <reason>`
- Developer / sysops: `READY FOR REVIEW: CCR-NNN` or `BLOCKED: CCR-NNN — <reason>`
- Team lead: `APPROVED: CCR-NNN` or `REJECTED: CCR-NNN — <one-line summary; details in TICKETS.md notes>` or `FEATURE COMPLETE: <feature-name>`

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
  - <YYYY-MM-DD> <agent>: <one-line note, e.g. "ruff failed on src/ccr/cli.py — sent back">
````

`<status>` is one of: `todo` `in-progress` `in-review` `done` `blocked`.

**Status goes inside `[...]` in the title** so it's grep-able as a single line:
```
grep -E '^## CCR-[0-9]+' TICKETS.md
```
returns the title + status without needing the line below.

### Status transitions

- `todo` → `in-progress` (main session marks it when dispatching to a dev)
- `in-progress` → `in-review` (developer marks it when emitting `READY FOR REVIEW`)
- `in-review` → `done` (team lead marks it on `APPROVED`)
- `in-review` → `in-progress` (team lead marks it on `REJECTED`, with a Review log entry)
- any → `blocked` (with a Review log entry explaining what is blocking)

## `.claude/docs/<feature>/`

One folder per feature. Slug is lowercase-hyphenated and names a capability, not a phase (e.g. `core`, `auth`, `chat-bot`, `web-viewer`). PM creates the folder + stubs when generating the first ticket for that feature; multiple tickets across phases can share one feature folder.

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

### `CONTEXT.md` (developers write / update)

Created as a stub by PM, maintained continuously by whichever developer touches the feature. Format:

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

The change-history entries are append-only. Developers must add an entry **in the same edit** that completes the ticket — before emitting `READY FOR REVIEW`.

## Phase ordering

The implementation plan (`claude-code-remote-plan.md`) has phase ordering caveats — most importantly **Phase 7 must land before Phase 6** even though Phase 6 is listed first. PM must read the full phase including any `⚠️ ordering note` callout before generating tickets and respect dependencies in the `Depends on:` field.

## Verification (team lead)

Team lead approves a ticket only after **executing** the acceptance criteria, not by reading the diff. Concrete checks:

- `pytest <path>` passes (or full `pytest` if criteria say so)
- `ruff check src tests` passes
- `ruff format --check src tests` passes
- `mypy src` passes
- Any literal command in the acceptance list runs and produces the expected exit code / output

If any check fails, mark the ticket `in-progress`, append to Review log, return `REJECTED: CCR-NNN — <reason>`.

## Integration: GitHub PR (never merge automatically)

After team lead emits `APPROVED: CCR-NNN`, the main session prepares a GitHub branch and pull request for the user to merge **manually**. Claude must **never** merge a PR, push to `main`, or run `gh pr merge` on the user's behalf.

### Branch naming

Rename the developer's worktree branch from `worktree-agent-<id>` to `ccr-NNN-<short-slug>` where the slug is a 1–4-word kebab-cased summary of the ticket title (e.g. `ccr-001-package-scaffold`, `ccr-002-ci-and-precommit`). The renamed branch is what gets pushed.

### Commit message

One commit per ticket on the renamed branch. Format:

```
CCR-NNN: <ticket title>

- <bullet: what was added or changed, mirroring the ticket Files list>
- ...

Acceptance: all <N> criteria verified by team lead (see TICKETS.md Review log).
```

Include in the commit: every file the ticket scoped, the ticket's own `TICKETS.md` status/Review-log update, and any `CONTEXT.md` change for the feature folder. Do **not** include unrelated edits (other tickets' status changes, stray config tweaks, etc.). If `pre-commit` or CI fails on push, fix the underlying issue and create a NEW commit — never `--amend` a published commit.

### PR body template

```markdown
## Summary
<1–3 bullets — what this ticket delivers in plain language>

## Files
- <path> — <one-line role>
- ...

## Acceptance
- [x] <verbatim ticket criterion> — verified by team lead
- [x] ...

## Ticket
TICKETS.md → CCR-NNN
```

### Steps

1. In the worktree, stage only the files this ticket owns (plus the ticket's `TICKETS.md` / `CONTEXT.md` updates): `git add -A` is fine if the worktree is clean of unrelated noise; otherwise stage by path.
2. Commit using the message format above.
3. Rename the branch: `git branch -m worktree-agent-<id> ccr-NNN-<slug>`.
4. Push: `git push -u origin ccr-NNN-<slug>`.
5. Open the PR: `gh pr create --base main --title "CCR-NNN: <title>" --body "<filled template>"`.
6. Report the PR URL back to the user.
7. **Stop.** The user reviews and merges the PR manually. Worktree cleanup (`git worktree remove`, branch delete) happens **after** the user merges, not before.

### Hard rules

- Never run `git merge`, `gh pr merge`, `git push origin main`, or `git push --force` against `main`.
- Never delete the worktree or its branch before the PR is merged — the user may want to push fixes onto it.
- If the PR's CI fails after push, push a follow-up commit on the same branch; do not force-push or amend.
- The user is the only one who decides when a PR lands.
