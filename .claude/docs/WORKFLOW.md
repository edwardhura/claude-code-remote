# Multi-Agent Workflow

This file is the shared protocol for the agents in `.claude/agents/`. Every agent reads it at the start of a run.

## Roles

| Agent | Writes code? | Primary outputs |
|---|---|---|
| `orchestrator` | No | Routing decisions; invokes other agents via the Agent tool |
| `project-manager` | No | Tickets in `TICKETS.md`; stub `.claude/docs/<feature>/{BRIEF,CONTEXT}.md` |
| `team-lead` | No | Ticket status updates in `TICKETS.md`; `BRIEF.md` on completion |
| `python-developer` | Yes | Code under `src/ccr/{bot,auth,claude,console,db,events}/`, tests, `CONTEXT.md` updates |
| `web-developer` | Yes | Code under `src/ccr/web/` (FastAPI, viewer frontend), tests, `CONTEXT.md` updates |
| `sysops` | Yes | `install.sh`, `.github/workflows/`, `doctor` subcommand, `.env.example`, `CONTEXT.md` updates |

## Handoffs (orchestrator routes everything)

Subagents do **not** invoke each other. When a subagent finishes, it returns a structured one-line verdict to the orchestrator. The orchestrator parses it and decides the next agent.

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

- `todo` → `in-progress` (orchestrator marks it when dispatching to a dev)
- `in-progress` → `in-review` (developer marks it when emitting `READY FOR REVIEW`)
- `in-review` → `done` (team lead marks it on `APPROVED`)
- `in-review` → `in-progress` (team lead marks it on `REJECTED`, with a Review log entry)
- any → `blocked` (with a Review log entry explaining what is blocking)

## `.claude/docs/<feature>/`

One folder per feature. Slug is lowercase-hyphenated (e.g. `phase-03-pairing-auth`, `phase-12-viewer-frontend`). PM creates the folder + stubs when generating the first ticket for that feature.

### `BRIEF.md` (team lead writes / updates)

Created as a stub by PM, filled in by team lead when the feature's last ticket is approved. Format:

```markdown
# Brief: <feature-name>

## What
<one short paragraph: capabilities delivered>

## Why
<one short paragraph: motivation, what problem this solves>

## Summary
- <bullet list of capabilities>

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
